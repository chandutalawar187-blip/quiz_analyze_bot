"""Authenticated live web dashboard for Quizcord sessions."""

import asyncio
import html
import logging
import secrets
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from quizcord_bot.config import Settings
from quizcord_bot.database import (
    Database,
    DashboardUser,
    QuizSessionSummary,
    StudentQuizAnalytics,
    matches_usn_format,
)
from quizcord_bot.exports import quiz_session_workbook, student_analytics_workbook


LOGGER = logging.getLogger("quizcord_analytics.dashboard")
DISCORD_API = "https://discord.com/api/v10"
MANAGE_GUILD_PERMISSION = 1 << 5
ADMINISTRATOR_PERMISSION = 1 << 3
OAUTH_SESSION_TTL_SECONDS = 3600
PERMISSION_CACHE_SECONDS = 60
RATE_LIMIT_MAX_BUCKETS = 10_000
RATE_LIMIT_RULES = {
    "/auth/login": (12, 60),
    "/auth/callback": (20, 60),
    "/api/faculty/password/verify": (10, 900),
    "/api/faculty/password/change": (10, 900),
    "/api/admin/faculty-passwords": (10, 60),
    "/api/faculty/candidates": (30, 60),
}
DEFAULT_REQUEST_RATE_LIMIT = (300, 60)
DEFAULT_API_RATE_LIMIT = (120, 60)


def _error_page(status_code: int, detail: Any) -> str:
    titles = {
        400: "That request didn't work",
        401: "You need to sign in",
        403: "You don't have permission",
        404: "This page is missing",
        405: "That action isn't available",
        409: "This request conflicts with the current state",
        422: "Check the information and try again",
        429: "Too many requests",
        502: "Discord couldn't complete the request",
        503: "This service is temporarily unavailable",
    }
    title = titles.get(status_code, "Something went wrong")
    message = html.escape(str(detail), quote=True)
    destination = "/login" if status_code == 401 else "/"
    link_label = "Sign in with Discord" if status_code == 401 else "Back to dashboard"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>{status_code} · Quizcord Analytics</title>
  <style>
    :root{{font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;color:#f2f3f5;background:#313338}}
    *{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:radial-gradient(ellipse at 50% 40%,#3b3d78 0,#313338 60%)}}
    main{{width:min(520px,calc(100% - 32px));padding:38px;border-radius:12px;background:#2b2d31;box-shadow:0 18px 50px #11131888;text-align:center}}
    .brand{{font-size:13px;font-weight:750;letter-spacing:.5px;color:#b5bac1;text-transform:uppercase}}.brand span{{color:#a6a9ff}}
    .code{{font-size:72px;line-height:1;font-weight:800;letter-spacing:-4px;color:#8c8fff;margin:32px 0 12px}}
    h1{{font-size:24px;margin:0 0 12px}}p{{color:#b5bac1;line-height:1.6;margin:0 auto 24px;overflow-wrap:anywhere}}
    a{{display:inline-block;padding:11px 17px;border-radius:5px;background:#5865f2;color:white;text-decoration:none;font-size:14px;font-weight:650}}
    a:hover{{background:#4752c4}}.hint{{font-size:12px;color:#949ba4;margin-top:22px}}
  </style>
</head>
<body>
  <main>
    <div class="brand">Quizcord <span>Analytics</span></div>
    <div class="code">{status_code}</div>
    <h1>{title}</h1>
    <p>{message}</p>
    <a href="{destination}">{link_label}</a>
    <div class="hint">If this keeps happening, contact your server administrator.</div>
  </main>
</body>
</html>"""


def _wants_json_error(request: Request) -> bool:
    if request.url.path.startswith("/api/"):
        return True
    accept = request.headers.get("accept", "").casefold()
    return "application/json" in accept and "text/html" not in accept


@dataclass
class _OAuthSession:
    user_id: int
    username: str
    access_token: str
    refresh_token: str
    access_token_expires_at: float
    expires_at: float
    last_permission_check: float
    is_admin: bool
    is_faculty: bool
    faculty_password_verified: bool
    must_change_faculty_password: bool
    permission_check_lock: asyncio.Lock


HTML_PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>Quizcord Analytics</title>
  <style>
    :root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;color:#e7edf8;background:#0b1020;font-synthesis:none}
    *{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(ellipse at 10% 0%,#17264a 0,transparent 42%),#0b1020}
    button,a{font:inherit}.top{height:72px;display:flex;align-items:center;justify-content:space-between;padding:0 max(24px,calc((100vw - 1280px)/2));border-bottom:1px solid #26314b;background:#0b1020cc;position:sticky;top:0;backdrop-filter:blur(14px);z-index:2}
    .brand{font-weight:750;letter-spacing:.2px}.brand span{color:#8fa7ff}.account{display:flex;gap:16px;align-items:center;color:#aebbd2;font-size:14px}.account a{color:#aebbd2;text-decoration:none}
    main{max-width:1280px;margin:38px auto;padding:0 24px}.heading{display:flex;justify-content:space-between;align-items:end;gap:20px;margin-bottom:24px}.eyebrow{color:#91a6ff;font-size:12px;text-transform:uppercase;letter-spacing:1.4px;font-weight:700}
    .dashboard-tabs{display:flex;gap:8px;margin-bottom:24px;border-bottom:1px solid #26314b}.dashboard-tabs button{padding:11px 16px;border:0;border-bottom:2px solid transparent;background:transparent;color:#91a0ba;font-weight:650;cursor:pointer}.dashboard-tabs button[aria-selected="true"]{color:#dce5f8;border-bottom-color:#8197ff}.directory-tools{display:flex;justify-content:space-between;align-items:center;gap:16px;padding:16px 20px;color:#95a4bd;font-size:13px}.directory-search{max-width:360px;width:100%;padding:10px 12px;background:#0b1020;color:#e7edf8;border:1px solid #33415e;border-radius:9px}.directory-table{width:100%;min-width:720px}.directory-warning{color:#ffbd59;font-size:11px;font-weight:650}
    h1{font-size:30px;margin:8px 0 0}select{color:#e7edf8;background:#141d32;border:1px solid #33415e;padding:11px 38px 11px 13px;border-radius:10px;min-width:240px}
    .grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px}.card{background:linear-gradient(145deg,#151f35,#11182b);border:1px solid #26334e;border-radius:16px;padding:20px;box-shadow:0 12px 32px #05081433}.metric-label{font-size:13px;color:#9baac3}.metric{font-size:28px;font-weight:750;margin-top:12px;letter-spacing:-.5px}.status{display:inline-flex;align-items:center;gap:8px;color:#b7c5dc;font-size:13px}.dot{width:8px;height:8px;border-radius:50%;background:#53d6a2;box-shadow:0 0 12px #53d6a277}.dot.complete{background:#7b91ff;box-shadow:0 0 12px #7b91ff77}.dot.interrupted{background:#ffbd59;box-shadow:0 0 12px #ffbd5977}
    .main-grid{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(280px,1fr);gap:16px;margin-top:16px}.panel{background:#11182b;border:1px solid #26334e;border-radius:16px;overflow:hidden}.panel-head{padding:19px 21px;border-bottom:1px solid #26314b;display:flex;justify-content:space-between;align-items:center}.panel h2{font-size:16px;margin:0}.panel-sub{font-size:12px;color:#8291ac;margin-top:5px}.table-wrap{overflow:auto;max-height:520px}table{border-collapse:collapse;width:100%;text-align:left}th,td{padding:13px 20px;border-bottom:1px solid #202b42;font-size:13px}th{color:#8797b3;font-size:11px;text-transform:uppercase;letter-spacing:.7px;position:sticky;top:0;background:#11182b}td.points{text-align:right;font-variant-numeric:tabular-nums;color:#dce5f8}.rank{color:#91a6ff;font-weight:700}.usn{font-weight:650;letter-spacing:.2px}.student-link{color:#dce5f8;text-decoration:none}.student-link:hover{color:#91a6ff;text-decoration:underline}.name-warning{color:#ffbd59;font-size:11px;line-height:1.3;margin-top:4px}.ring-wrap{display:grid;place-items:center;padding:26px}.ring{width:156px;height:156px;position:relative;display:grid;place-items:center}.ring svg{position:absolute;inset:0;transform:rotate(-90deg)}.ring circle{fill:none;stroke-width:9}.ring .track{stroke:#283653}.ring .value{stroke:#8197ff;stroke-linecap:round;stroke-dasharray:326.7;stroke-dashoffset:326.7;transition:stroke-dashoffset .5s}.ring.complete .value{stroke-dashoffset:0}.ring-center{text-align:center;position:relative;z-index:1;max-width:100%;padding:0 12px}.ring-number{font-size:32px;font-weight:750}.ring-caption{font-size:11px;color:#95a4bd;margin-top:3px;white-space:nowrap}.summary-list{padding:0 20px 18px}.summary-row{padding:12px 0;border-bottom:1px solid #202b42;display:flex;justify-content:space-between;gap:16px;font-size:13px}.summary-row:last-child{border:0}.summary-row span:first-child{color:#91a0ba}.summary-row strong{text-align:right;font-variant-numeric:tabular-nums}.note{color:#7f8da7;font-size:12px;line-height:1.6;padding:0 20px 20px}    .empty{color:#8797b3;text-align:center;padding:46px 20px;font-size:14px}.error{color:#ff9e9e}.refresh{color:#71809b;font-size:12px}.profile-heading{margin-bottom:24px;display:flex;justify-content:space-between;align-items:center;gap:18px}.export-link{display:inline-block;background:#26365b;color:#dce5ff;text-decoration:none;padding:11px 16px;border:1px solid #3b4d77;border-radius:9px;font-size:13px;font-weight:650;white-space:nowrap}.export-link:hover{background:#344875}.profile-grid{display:grid;grid-template-columns:minmax(240px,.7fr) minmax(0,1.8fr);gap:16px;margin-top:16px}.profile-ring{display:grid;place-items:center;padding:20px}.profile-ring .ring{width:190px;height:190px}.profile-ring .ring-number{font-size:36px}.profile-bars{padding:4px 20px 18px}.bar-row{display:grid;grid-template-columns:100px 1fr 64px;align-items:center;gap:12px;margin:13px 0;font-size:12px;color:#9baac3}.bar-track{height:9px;border-radius:8px;background:#26334e;overflow:hidden}.bar-track span{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,#5865f2,#55d6b0)}.back-link{color:#91a6ff;text-decoration:none;font-size:13px}.back-link:hover{text-decoration:underline}.empty{color:#8797b3;text-align:center;padding:46px 20px;font-size:14px}.error{color:#ff9e9e}.refresh{color:#71809b;font-size:12px}.login{max-width:500px;margin:12vh auto;padding:20px}.login .card{padding:34px}.login a.button{display:inline-block;background:#5865f2;color:white;text-decoration:none;padding:12px 18px;border-radius:10px;font-weight:650;margin-top:18px}
    @media(max-width:860px){.grid{grid-template-columns:repeat(2,1fr)}.main-grid{grid-template-columns:1fr}}@media(max-width:520px){.top{padding:0 16px}.account{gap:8px;font-size:12px}main{margin:24px auto;padding:0 16px}.heading{align-items:start;flex-direction:column}select{width:100%}.grid{gap:10px}.card{padding:16px}.metric{font-size:24px}th,td{padding:12px 14px}}
  </style>
</head>
<body>
  <header class="top"><div class="brand">Quizcord <span>Analytics</span></div><div class="account"><a id="back-link" href="/" hidden>← Dashboard</a><a id="faculty-password-link" href="/admin/faculty-passwords" hidden>Faculty passwords</a><span id="account-name"></span><a href="/auth/logout">Sign out</a></div></header>
  <main>
    <nav class="dashboard-tabs" id="dashboard-tabs" aria-label="Dashboard sections">
      <button type="button" data-view="sessions" aria-selected="true">Quiz sessions</button>
      <button type="button" data-view="users" aria-selected="false">All users</button>
    </nav>
    <section class="heading" id="session-heading"><div><div class="eyebrow">Live monitoring</div><h1>Quiz dashboard</h1></div><select id="session-select" aria-label="Select quiz session"></select></section>
    <div id="content"><div class="empty">Loading quiz sessions…</div></div>
  </main>
  <script>
    const select = document.getElementById("session-select");
    const content = document.getElementById("content");
    const dashboardTabs = document.getElementById("dashboard-tabs");
    const sessionHeading = document.getElementById("session-heading");
    const initialSession = new URLSearchParams(location.search).get("session");
    const facultyStudentPath = location.pathname.startsWith("/faculty/students/");
    const studentPath = facultyStudentPath ? location.pathname.slice("/faculty/students/".length) : location.pathname.startsWith("/students/") ? location.pathname.slice("/students/".length) : null;
    const studentUSN = studentPath ? decodeURIComponent(studentPath) : null;
    const safe = value => String(value ?? "");
    const el = (tag, className, text) => { const node=document.createElement(tag); if(className)node.className=className; if(text!==undefined)node.textContent=text; return node; };
    let activeSession = initialSession;
    let activeView = "sessions";
    let pollBusy = false;
    function metric(label,value){const card=el("div","card");card.append(el("div","metric-label",label),el("div","metric",value));return card;}
    function statusLabel(status){return status==="completed"?"Complete":status==="interrupted"?"Interrupted":"Live";}
    function dateLabel(value,withTime=false){if(!value)return"Date unavailable";if(/^\d{4}-\d{2}-\d{2}$/.test(value)){const [year,month,day]=value.split("-").map(Number);return new Date(year,month-1,day).toLocaleDateString();}return new Date(value).toLocaleString(undefined,withTime?{}:{dateStyle:"short"});}
    function render(summary){
      const session=summary.session;
      const complete=session.status==="completed";
      const participants=summary.participants||[];
      const grid=el("section","grid");
      const statusCard=el("div","card");
      statusCard.append(el("div","metric-label","Session status"));
      const state=el("div","metric status");
      const dot=el("span","dot"+(complete?" complete":session.status==="interrupted"?" interrupted":""));
      state.append(dot,document.createTextNode(statusLabel(session.status)));
      statusCard.append(state);grid.append(statusCard);
      const attendanceCount=summary.attendance_count||0;
      const eligibleCount=summary.eligible_student_count;
      const attendanceRate=summary.attendance_percentage;
      grid.append(metric(complete?"Students attended":"Students attending",attendanceCount+" / "+(eligibleCount??"—")),metric("Attendance rate",attendanceRate===null?"—":Number(attendanceRate).toFixed(1)+"%"),metric("Leaderboard updates",summary.question_count),metric("Quiz ID",session.quizcord_quiz_id||"Pending"));
      const layout=el("section","main-grid");
      const roster=el("article","panel");
      const header=el("div","panel-head");
      const title=el("div");title.append(el("h2","", "Participant leaderboard"),el("div","panel-sub",complete?"Final leaderboard snapshot":"Latest leaderboard snapshot"));
      header.append(title,el("span","refresh","Updates automatically"));roster.append(header);
      if(!participants.length){roster.append(el("div","empty","No participant rows have arrived yet."));}
      else{
        const wrap=el("div","table-wrap"),table=el("table"),thead=el("thead"),tr=el("tr");
        ["Rank","Student USN","Quizcord points"].forEach(label=>tr.append(el("th","",label)));
        thead.append(tr);table.append(thead);const tbody=el("tbody");
        participants.forEach(person=>{const row=el("tr"),link=document.createElement("a");link.className="student-link";link.href="/students/"+encodeURIComponent(person.usn);link.textContent=person.usn;row.append(el("td","rank","#"+safe(person.rank)));const studentCell=el("td","usn");studentCell.append(link);if(person.name_matches_usn_format===false)studentCell.append(el("div","name-warning","Name not updated to USN"));row.append(studentCell,el("td","points",safe(person.points)));tbody.append(row);});
        table.append(tbody);wrap.append(table);roster.append(wrap);
      }
      const side=el("article","panel");const sideHead=el("div","panel-head");sideHead.append(el("h2","","Student attendance"));side.append(sideHead);
      const ringWrap=el("div","ring-wrap"),ring=el("div","ring"+(complete?" complete":""));
      ring.innerHTML='<svg viewBox="0 0 120 120" aria-hidden="true"><circle class="track" cx="60" cy="60" r="52"></circle><circle class="value" cx="60" cy="60" r="52"></circle></svg>';
      const ringValue=ring.querySelector(".value");
      if(ringValue&&attendanceRate!==null)ringValue.style.strokeDashoffset=String(326.7*(1-Math.min(100,attendanceRate)/100));
      const center=el("div","ring-center");center.append(el("div","ring-number",attendanceRate===null?"—":Number(attendanceRate).toFixed(1)+"%"),el("div","ring-caption",eligibleCount===null?"Member count unavailable":attendanceCount+" / "+eligibleCount+" attended"));ring.append(center);ringWrap.append(ring);side.append(ringWrap);
      const rows=el("div","summary-list");
      [["Human server members",eligibleCount??"Unavailable"],["Started",new Date(session.started_at).toLocaleString()]].forEach(([label,value])=>{const row=el("div","summary-row");row.append(el("span","",label),el("strong","",safe(value)));rows.append(row);});
      side.append(rows,el("div","note","Attendance includes human members regardless of server permissions. Bot accounts are excluded."));
      if(complete){const link=document.createElement("a");link.href="/api/sessions/"+encodeURIComponent(session.session_id)+"/export.xlsx";link.textContent="Download Excel";link.style.cssText="display:block;margin:0 20px 20px;text-align:center;padding:11px;border-radius:9px;background:#26365b;color:#dce5ff;text-decoration:none;font-size:13px;font-weight:650";side.append(link);}
      layout.append(roster,side);content.replaceChildren(grid,layout);
    }
    function renderUsers(users){
      const heading=el("section","heading"),title=el("div","");
      title.append(el("div","eyebrow","Server directory"),el("h1","","All users"));
      heading.append(title,el("div","panel-sub",users.length+" human members"));
      const panel=el("article","panel"),tools=el("div","directory-tools");
      const search=document.createElement("input");search.className="directory-search";search.type="search";search.placeholder="Search names or USN";search.setAttribute("aria-label","Search users");
      tools.append(el("span","","Names marked below need a USN-format global display name."),search);panel.append(tools);
      const wrap=el("div","table-wrap"),table=el("table","directory-table"),thead=el("thead"),headRow=el("tr");
      ["Global display name","Account username","Server nickname","Student name","USN"].forEach(label=>headRow.append(el("th","",label)));
      thead.append(headRow);table.append(thead);const body=el("tbody");
      const rows=[];
      users.forEach(user=>{const row=el("tr"),globalCell=el("td","");
        globalCell.append(el("span","",user.global_name||"Not set"));
        if(!user.name_matches_usn_format)globalCell.append(el("div","directory-warning","Name not updated to USN"));
        row.append(globalCell,el("td","",user.account_name),el("td","",user.server_nickname||"—"),el("td","",user.student_name||"Not mapped"),el("td","usn",user.usn||"—"));
        row.dataset.search=[user.global_name,user.account_name,user.server_nickname,user.student_name,user.usn].filter(Boolean).join(" ").toLocaleLowerCase();
        rows.push(row);body.append(row);
      });
      table.append(body);wrap.append(table);panel.append(wrap);
      if(!users.length)panel.append(el("div","empty","No synchronized human members found."));
      search.addEventListener("input",()=>{const query=search.value.trim().toLocaleLowerCase();rows.forEach(row=>{row.hidden=!row.dataset.search.includes(query);});});
      content.replaceChildren(heading,panel);
    }
    function renderStudent(student){
      document.getElementById("back-link").hidden=false;
      document.getElementById("back-link").href=facultyStudentPath?"/faculty":"/";
      select.hidden=true;
      document.querySelector(".eyebrow").textContent="Student analytics";
      document.querySelector("h1").textContent=student.student_name;
      const heading=el("div","profile-heading");
      const headingDetails=el("div","");
      headingDetails.append(el("div","eyebrow",student.usn),el("div","panel-sub","Quiz attendance and performance across completed quizzes tracked by this bot."));
      const exportLink=document.createElement("a");exportLink.className="export-link";exportLink.href="/api/students/"+encodeURIComponent(student.usn)+"/export.xlsx";exportLink.textContent="Download Excel";heading.append(headingDetails,exportLink);
      const grid=el("section","grid");
      grid.append(metric("Quizzes attended",student.quizzes_attended),metric("Completed quizzes tracked",student.completed_quizzes),metric("Attendance",student.attendance_percentage.toFixed(1)+"%"),metric("Total Quizcord points",student.total_points),metric("Average points per attended quiz",student.average_points===null?"—":Number(student.average_points).toFixed(1)));
      const profile=el("section","profile-grid");
      const attendance=el("article","panel");
      const attendanceHead=el("div","panel-head");attendanceHead.append(el("h2","","Attendance rate"));attendance.append(attendanceHead);
      const ringWrap=el("div","profile-ring"),ring=el("div","ring"),svg=document.createElementNS("http://www.w3.org/2000/svg","svg");
      svg.setAttribute("viewBox","0 0 120 120");svg.setAttribute("aria-hidden","true");
      for(const className of ["track","value"]){const circle=document.createElementNS("http://www.w3.org/2000/svg","circle");circle.setAttribute("class",className);circle.setAttribute("cx","60");circle.setAttribute("cy","60");circle.setAttribute("r","52");svg.append(circle);if(className==="value")circle.style.strokeDashoffset=String(326.7*(1-Math.min(100,student.attendance_percentage)/100));}
      const center=el("div","ring-center");center.append(el("div","ring-number",student.attendance_percentage.toFixed(1)+"%"),el("div","ring-caption",student.quizzes_attended+" of "+student.completed_quizzes+" quizzes"));ring.append(svg,center);ringWrap.append(ring);attendance.append(ringWrap,el("div","note","Attendance percentage includes tracked quiz sessions and manual imports. Imports without a saved date remain marked Date unavailable."));
      const performance=el("article","panel"),performanceHead=el("div","panel-head");performanceHead.append(el("h2","","Points by quiz"));performance.append(performanceHead);
      const completedHistory=student.history.filter(item=>item.status==="completed"||item.status==="historical");
      if(completedHistory.length){const bars=el("div","profile-bars"),maxPoints=Math.max(1,...completedHistory.map(item=>item.points));completedHistory.slice().reverse().forEach(item=>{const row=el("div","bar-row");row.append(el("span","",dateLabel(item.started_at)));const track=el("div","bar-track"),bar=el("span");bar.style.width=(item.points/maxPoints*100)+"%";track.append(bar);row.append(track,el("strong","",String(item.points)));bars.append(row);});performance.append(bars);}else{performance.append(el("div","empty","No completed quiz scores recorded yet."));}
      profile.append(attendance,performance);
      const historyPanel=el("article","panel"),historyHead=el("div","panel-head");historyHead.append(el("div","","Date-wise attendance and results"));historyPanel.append(historyHead);
      if(!student.history.length){historyPanel.append(el("div","empty","No quiz attendance has been recorded for this student."));}
      else{const wrap=el("div","table-wrap"),table=el("table"),thead=el("thead"),headRow=el("tr");["Date","Quiz ID","Status","Rank","Points"].forEach(label=>headRow.append(el("th","",label)));thead.append(headRow);table.append(thead);const body=el("tbody");student.history.forEach(item=>{const row=el("tr"),date=dateLabel(item.started_at,true),status=item.status==="completed"?"Complete":item.status==="active"?"Live":item.status==="historical"?"Historical import":"Interrupted";row.append(el("td","",date),el("td","",item.quizcord_quiz_id||"Not provided"),el("td","",status),el("td","rank",item.rank?"#"+safe(item.rank):"—"),el("td","points",safe(item.points)));body.append(row);});table.append(body);wrap.append(table);historyPanel.append(wrap);}
      content.replaceChildren(heading,grid,profile,historyPanel);
    }
    async function refresh(){
      if(pollBusy||activeView!=="sessions")return;pollBusy=true;
      try{
        const response=await fetch("/api/sessions",{cache:"no-store"});
        if(response.status===401){location.replace("/");return;}
        if(!response.ok)throw new Error("Could not load quiz sessions.");
        const sessions=await response.json();
        const old=activeSession||select.value;
        select.replaceChildren();
        sessions.forEach(session=>{const option=document.createElement("option");option.value=session.session_id;option.textContent=(session.quizcord_quiz_id||"Quiz session")+" · "+statusLabel(session.status)+" · "+new Date(session.started_at).toLocaleString();select.append(option);});
        if(!sessions.length){content.replaceChildren(el("div","empty","No quiz sessions have been recorded yet. Start a Quizcord quiz to see its live dashboard."));return;}
        activeSession=sessions.some(s=>s.session_id===old)?old:sessions[0].session_id;select.value=activeSession;
        const detail=await fetch("/api/sessions/"+encodeURIComponent(activeSession),{cache:"no-store"});
        if(!detail.ok)throw new Error("Could not load the selected quiz.");
        const summary=await detail.json();
        if(activeView==="sessions")render(summary);
      }catch(error){content.replaceChildren(el("div","empty error",error.message));}
      finally{pollBusy=false;}
    }
    async function refreshUsers(){
      if(pollBusy||activeView!=="users")return;pollBusy=true;
      try{
        const response=await fetch("/api/users",{cache:"no-store"});
        if(response.status===401){location.replace("/");return;}
        if(!response.ok)throw new Error("Could not load server users.");
        const users=await response.json();
        if(activeView==="users")renderUsers(users);
      }catch(error){content.replaceChildren(el("div","empty error",error.message));}
      finally{pollBusy=false;}
    }
    function switchView(view){
      activeView=view;
      dashboardTabs.querySelectorAll("button").forEach(button=>button.setAttribute("aria-selected",String(button.dataset.view===view)));
      sessionHeading.hidden=view!=="sessions";
      select.hidden=view!=="sessions";
      if(view==="users")refreshUsers();else refresh();
    }
    dashboardTabs.addEventListener("click",event=>{const button=event.target.closest("button[data-view]");if(button)switchView(button.dataset.view);});
    select.addEventListener("change",()=>{activeSession=select.value;refresh();});
    fetch("/api/me").then(response=>{if(!response.ok)throw new Error("Sign-in expired.");return response.json();}).then(user=>{document.getElementById("account-name").textContent=user.username;document.getElementById("faculty-password-link").hidden=user.role!=="admin";}).catch(()=>location.replace("/"));
    if(studentUSN){
      dashboardTabs.hidden=true;
      select.hidden=true;
      async function refreshStudent(){try{const endpoint=facultyStudentPath?"/api/faculty/students/":"/api/students/";const response=await fetch(endpoint+encodeURIComponent(studentUSN),{cache:"no-store"});if(response.status===401){location.replace(facultyStudentPath?"/faculty/login":"/");return;}if(!response.ok)throw new Error("Could not load student analytics.");renderStudent(await response.json());}catch(error){content.replaceChildren(el("div","empty error",error.message));}}
      refreshStudent();setInterval(refreshStudent,10000);
    }else{refresh();setInterval(()=>{if(activeView==="users")refreshUsers();else refresh();},3000);}
  </script>
</body>
</html>
"""

FACULTY_LOGIN_PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>Faculty sign in · Quizcord Analytics</title>
  <style>
    :root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;color:#182033;background:#f6f7fa;font-synthesis:none}
    *{box-sizing:border-box}
    body{margin:0;min-height:100vh;display:grid;grid-template-columns:minmax(380px,560px) minmax(0,1fr);background:#f6f7fa}
    a{color:inherit}
    .identity-panel{min-height:100vh;display:flex;flex-direction:column;justify-content:space-between;padding:56px 52px 50px 68px;background:#0e1729;color:#f5f7fc}
    .brand-lockup{display:flex;align-items:center;gap:12px}
    .brand-mark{width:38px;height:38px;display:grid;place-items:center;border:1px solid #4f669c;border-radius:10px;background:#293b63;color:#cfd9ff;font-size:17px;font-weight:800}
    .brand-copy{display:flex;flex-direction:column}
    .brand-name{font-size:13px;font-weight:800;letter-spacing:1.3px}
    .brand-descriptor{margin-top:2px;color:#9eaccb;font-size:9px;letter-spacing:1.7px}
    .faculty-story{width:min(100%,430px);margin:10vh 0}
    .eyebrow{color:#a9bfff;font-size:11px;font-weight:700;letter-spacing:1.8px}
    .faculty-story h1{margin:18px 0 16px;font-size:44px;line-height:1.18;letter-spacing:-1.6px;color:#f5f7fc}
    .faculty-story p{max-width:420px;margin:0;color:#b8c4dc;font-size:14px;line-height:1.8}
    .steps{display:grid;gap:22px;width:min(100%,420px)}
    .step{display:flex;align-items:flex-start;gap:14px}
    .step-number{flex:none;width:34px;height:34px;display:grid;place-items:center;border:1px solid #45577d;border-radius:50%;background:#212f4f;color:#bac8f0;font-size:11px;font-weight:700}
    .step-title{margin:0 0 4px;color:#eff3fc;font-size:14px;font-weight:650}
    .step-detail{margin:0;color:#aebbd2;font-size:12px;line-height:1.55}
    .assurance{display:flex;align-items:center;gap:9px;color:#929fba;font-size:11px}
    .assurance-dot,.privacy-dot{flex:none;width:7px;height:7px;border-radius:50%;background:#73cbb0}
    .auth-panel{min-width:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:space-between;padding:38px 54px 36px;background:#f6f7fa}
    .mobile-brand{display:none}
    .admin-nav{width:min(100%,772px);display:flex;justify-content:flex-end}
    .admin-nav a{color:#586276;font-size:12px;text-decoration:none}
    .admin-nav a:hover{text-decoration:underline}
    .admin-nav strong{color:#475ba9;font-weight:700}
    .auth-card{width:min(100%,520px);padding:42px 42px 34px;border:1px solid #e0e5ee;border-radius:16px;background:#fff;box-shadow:0 14px 40px #17233b0b}
    .card-eyebrow{color:#5265ba;font-size:10px;font-weight:800;letter-spacing:1.6px}
    .auth-card h2{margin:12px 0 8px;color:#192238;font-size:30px;line-height:1.28;letter-spacing:-.8px}
    .card-description{margin:0;color:#626d81;font-size:14px;line-height:1.65}
    .discord-button{min-height:56px;width:100%;margin-top:26px;display:flex;align-items:center;justify-content:center;gap:11px;border:0;border-radius:9px;background:#4f63cc;color:#fff;text-decoration:none;font-size:15px;font-weight:700;transition:background-color .15s,transform .15s,box-shadow .15s}
    .discord-button:hover{transform:translateY(-1px);background:#4357bd;box-shadow:0 8px 18px #4357bd2e}
    .discord-button:focus-visible,.admin-nav a:focus-visible{outline:3px solid #8296ef;outline-offset:3px}
    .discord-icon{width:20px;height:20px;fill:currentColor}
    .role-note{margin:17px 0 0;color:#707a8d;font-size:12px;line-height:1.55}
    .divider{height:1px;margin:18px 0;background:#e6e9ef}
    .next-step{display:flex;align-items:flex-start;gap:13px;padding:14px;border:1px solid #e1e5f0;border-radius:9px;background:#f2f4fa}
    .next-number{flex:none;width:28px;height:28px;display:grid;place-items:center;border-radius:50%;background:#e3e8fb;color:#40539e;font-size:12px;font-weight:700}
    .next-title{margin:0 0 4px;color:#222b3d;font-size:13px;font-weight:700}
    .next-detail{margin:0;color:#697286;font-size:12px;line-height:1.5}
    .privacy-note{display:flex;align-items:center;gap:8px;color:#7d8799;font-size:12px}
    .privacy-dot{width:6px;height:6px}
    @media(max-width:900px){
      body{grid-template-columns:1fr}
      .identity-panel{display:none}
      .auth-panel{min-height:100vh;padding:28px 24px 26px}
      .mobile-brand{width:min(100%,520px);display:flex;align-items:center;gap:10px}
      .mobile-brand .brand-mark{border-color:#dbe2fa;background:#e8edff;color:#5265ba}
      .admin-nav{width:min(100%,520px)}
      .auth-card{margin:auto}
      .privacy-note{margin-top:24px}
    }
    @media(max-width:520px){
      .auth-panel{padding:22px 16px}
      .auth-card{padding:30px 24px 26px;border-radius:13px}
      .mobile-brand{margin-bottom:30px}
      .auth-card h2{font-size:27px}
      .privacy-note{font-size:11px;text-align:center}
    }
    @media(prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;transition:none!important}}
  </style>
</head>
<body>
  <aside class="identity-panel" aria-label="Quizcord faculty portal information">
    <div class="brand-lockup" aria-label="Quizcord Analytics">
      <span class="brand-mark" aria-hidden="true">Q</span>
      <span class="brand-copy"><span class="brand-name">QUIZCORD</span><span class="brand-descriptor">ANALYTICS</span></span>
    </div>
    <div>
      <section class="faculty-story">
        <div class="eyebrow">FACULTY PORTAL</div>
        <h1>A clearer view of every learner.</h1>
        <p>Review quiz participation and student progress in a workspace built for your faculty role.</p>
      </section>
      <div class="steps" aria-label="Sign-in steps">
        <div class="step">
          <span class="step-number" aria-hidden="true">01</span>
          <div><h2 class="step-title">Verify your identity</h2><p class="step-detail">Sign in with Discord and confirm your Faculty role.</p></div>
        </div>
        <div class="step">
          <span class="step-number" aria-hidden="true">02</span>
          <div><h2 class="step-title">Enter your faculty password</h2><p class="step-detail">Use the app password provided by your administrator.</p></div>
        </div>
      </div>
    </div>
    <div class="assurance"><span class="assurance-dot" aria-hidden="true"></span><span>Secure access for your configured Discord server.</span></div>
  </aside>
  <main class="auth-panel">
    <div class="mobile-brand" aria-label="Quizcord Analytics">
      <span class="brand-mark" aria-hidden="true">Q</span><span class="brand-name">QUIZCORD ANALYTICS</span>
    </div>
    <nav class="admin-nav" aria-label="Administrator sign in">
      <a href="/login">Administrator? <strong>Sign in to the admin portal</strong> →</a>
    </nav>
    <section class="auth-card">
      <div class="card-eyebrow">FACULTY SIGN-IN</div>
      <h2>Welcome, faculty.</h2>
      <p class="card-description">Continue with Discord to confirm your account and Faculty role.</p>
      <a class="discord-button" href="/auth/login?destination=faculty">
        <svg class="discord-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M19.7 5.3a18.4 18.4 0 0 0-4.6-1.4l-.6 1.2a16.7 16.7 0 0 0-5 0l-.6-1.2a18.4 18.4 0 0 0-4.6 1.4C1.4 9.5.6 13.6 1 17.6a18.5 18.5 0 0 0 5.7 2.9l1.2-2a11.8 11.8 0 0 1-1.9-.9l.5-.4a13.1 13.1 0 0 0 11 0l.5.4a11.8 11.8 0 0 1-1.9.9l1.2 2a18.5 18.5 0 0 0 5.7-2.9c.5-4.7-.8-8.8-3.3-12.3ZM8.8 14.9c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2Zm6.4 0c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2Z"/></svg>
        Continue with Discord
      </a>
      <p class="role-note">Only members with your server’s Faculty role can continue.</p>
      <div class="divider" aria-hidden="true"></div>
      <div class="next-step">
        <span class="next-number" aria-hidden="true">2</span>
        <div><h3 class="next-title">Next: enter your faculty password</h3><p class="next-detail">Your administrator provides this app password separately.</p></div>
      </div>
    </section>
    <div class="privacy-note"><span class="privacy-dot" aria-hidden="true"></span><span>Quizcord never asks for or stores your Discord password.</span></div>
  </main>
</body>
</html>"""

FACULTY_PASSWORD_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark"><title>Faculty password · Quizcord</title>
<style>
:root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;color:#f2f3f5;background:#313338}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:radial-gradient(ellipse at 50% 35%,#3b3d78 0,#313338 62%)}
main{width:min(460px,calc(100% - 32px));padding:36px;border-radius:12px;background:#2b2d31;box-shadow:0 18px 50px #11131888}
.brand{font-weight:750;letter-spacing:.5px;text-transform:uppercase;color:#b5bac1}.brand span{color:#a6a9ff}.eyebrow{margin-top:32px;color:#a6a9ff;font-size:12px;letter-spacing:1.4px;font-weight:700;text-transform:uppercase}
h1{font-size:26px;margin:8px 0 10px}.description{color:#b5bac1;line-height:1.55;margin:0 0 22px}
label{display:block;font-size:13px;color:#dbdee1;margin:14px 0 7px}input{width:100%;padding:12px;background:#1e1f22;border:1px solid #4e5058;border-radius:5px;color:#f2f3f5;font:inherit}
button{width:100%;margin-top:18px;padding:12px;border:0;border-radius:5px;background:#5865f2;color:white;font:inherit;font-weight:700;cursor:pointer}button:hover{background:#4752c4}
.muted{color:#949ba4;font-size:12px;line-height:1.5}.message{min-height:20px;margin-top:12px;color:#ff9e9e;font-size:13px}.hidden{display:none!important}a{color:#c4c9ce;font-size:13px}
</style></head><body><main>
<div class="brand">Quizcord <span>Faculty</span></div><div class="eyebrow">Second step</div>
<h1 id="title">Faculty password</h1>
<p class="description" id="description">Enter the temporary faculty password provided by your administrator.</p>
<form id="password-form"><div id="captcha-container"><p class="muted">Complete this security check to continue.</p><div id="turnstile-widget" data-sitekey="__TURNSTILE_SITE_KEY__"></div></div>
<label for="password">Faculty password</label><input id="password" type="password" autocomplete="current-password" disabled required>
<div id="new-password-fields" class="hidden"><label for="new-password">New password</label><input id="new-password" type="password" autocomplete="new-password" minlength="12" maxlength="128"><label for="confirm-password">Confirm new password</label><input id="confirm-password" type="password" autocomplete="new-password" minlength="12" maxlength="128"><p class="muted">Use 12–128 characters. No character-mix rules are required. This is an app password, not your Discord password.</p></div>
<button id="submit" type="submit" disabled>Continue</button><div class="message" id="message" role="alert"></div></form>
<a href="/auth/logout">Sign out</a>
<script>
const form=document.getElementById("password-form"),password=document.getElementById("password"),fields=document.getElementById("new-password-fields"),captchaContainer=document.getElementById("captcha-container"),captchaWidget=document.getElementById("turnstile-widget"),message=document.getElementById("message"),button=document.getElementById("submit");
let mustChange=false,turnstileWidgetId=null,captchaToken="";
async function send(path,data){const response=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data),cache:"no-store"});const body=await response.json().catch(()=>({detail:"Request failed."}));if(response.status===401&&body.detail==="Please sign in again."){location.replace("/faculty/login");throw new Error(body.detail);}if(!response.ok){const error=new Error(body.detail||"Could not verify your password.");error.body=body;throw error;}return body;}
function setVerificationEnabled(enabled){password.disabled=!enabled;button.disabled=!enabled;}
function resetCaptcha(){captchaToken="";setVerificationEnabled(false);if(turnstileWidgetId!==null&&window.turnstile)window.turnstile.reset(turnstileWidgetId);}
function showCaptcha(){captchaContainer.classList.remove("hidden");if(!captchaWidget.dataset.sitekey)throw new Error("Security check is not configured. Contact an administrator.");if(turnstileWidgetId!==null)return Promise.resolve();return new Promise((resolve,reject)=>{const render=()=>{turnstileWidgetId=window.turnstile.render(captchaWidget,{sitekey:captchaWidget.dataset.sitekey,action:"faculty_password",callback:token=>{captchaToken=token;setVerificationEnabled(true);message.textContent="";},"expired-callback":()=>{captchaToken="";setVerificationEnabled(false);},"error-callback":()=>{captchaToken="";setVerificationEnabled(false);message.textContent="Security check failed to load. Please retry.";}});resolve();};if(window.turnstile){render();return;}const script=document.createElement("script");script.src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";script.async=true;script.defer=true;script.onload=render;script.onerror=()=>reject(new Error("Security check failed to load. Please retry."));document.head.appendChild(script);});}
showCaptcha().catch(error=>{message.textContent=error.message;});
form.addEventListener("submit",async event=>{event.preventDefault();message.textContent="";button.disabled=true;
try{if(!mustChange){if(!captchaToken)throw new Error("Complete the security check before continuing.");const result=await send("/api/faculty/password/verify",{password:password.value,turnstile_token:captchaToken});captchaToken="";mustChange=result.must_change;if(mustChange){captchaContainer.classList.add("hidden");fields.classList.remove("hidden");password.value="";password.autocomplete="new-password";password.disabled=false;document.getElementById("title").textContent="Set a new password";document.getElementById("description").textContent="Your temporary password was accepted. Choose a new password to finish signing in.";button.textContent="Update password";button.disabled=false;return;}location.replace("/faculty");return;}
const next=document.getElementById("new-password").value,confirm=document.getElementById("confirm-password").value;if(next.length<12)throw new Error("Use at least 12 characters.");if(next!==confirm)throw new Error("The new passwords do not match.");await send("/api/faculty/password/change",{current_password:password.value,new_password:next});location.replace("/faculty");
}catch(error){message.textContent=error.message;if(!mustChange)resetCaptcha();else button.disabled=false;}});
</script></main></body></html>"""

FACULTY_ADMIN_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark"><title>Faculty passwords · Quizcord</title>
<style>
:root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;color:#e7edf8;background:#0b1020}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(ellipse at 10% 0%,#17264a 0,transparent 42%),#0b1020}
header{border-bottom:1px solid #26314b;background:#0e1528cc}
.header-inner{max-width:980px;margin:auto;padding:18px 24px;display:flex;align-items:center;justify-content:space-between;gap:16px}
.brand{font-size:16px;font-weight:800;letter-spacing:.3px}.brand span{color:#8fa7ff}
.admin-badge{display:inline-flex;align-items:center;gap:8px;padding:7px 11px;border:1px solid #40528a;border-radius:999px;background:#18264a;color:#c9d5ff;font-size:11px;font-weight:800;letter-spacing:1px}
.admin-badge::before{content:"";width:8px;height:8px;border-radius:50%;background:#91a6ff;box-shadow:0 0 12px #91a6ff88}
main{max-width:980px;margin:40px auto;padding:0 24px}.back{display:inline-flex;align-items:center;gap:8px;margin:0 0 20px;color:#aebbd2;text-decoration:none;font-size:14px}.back:hover{color:#fff}
.panel{background:linear-gradient(145deg,#131d34,#101729 70%);border:1px solid #2d3c60;border-radius:18px;padding:clamp(24px,5vw,42px);box-shadow:0 24px 70px #05091466}
.eyebrow{display:flex;align-items:center;gap:9px;color:#9db0ff;font-size:12px;letter-spacing:1.5px;text-transform:uppercase;font-weight:800}
.eyebrow::before{content:"";width:24px;height:1px;background:#7188ed}
h1{font-size:clamp(28px,4vw,38px);letter-spacing:-.7px;margin:12px 0 10px}
.description,.note{color:#aebbd2;line-height:1.65}.description{max-width:720px;margin:0}
.form-card{margin-top:30px;padding:22px;border:1px solid #273655;border-radius:13px;background:#0d1425}
label{display:block;margin:0 0 9px;font-size:13px;font-weight:650;color:#dce4f8}
input{width:min(100%,520px);padding:13px 14px;background:#090f1e;color:#e7edf8;border:1px solid #3a4969;border-radius:9px;font:inherit;transition:border-color .15s,box-shadow .15s}
input:focus{outline:none;border-color:#8296ff;box-shadow:0 0 0 3px #5865f233}
button{margin-top:16px;padding:12px 17px;border:0;border-radius:9px;background:linear-gradient(135deg,#6878ff,#5262e8);color:white;font:inherit;font-weight:750;cursor:pointer;box-shadow:0 7px 20px #4659ed33}
button:hover:not(:disabled){filter:brightness(1.1)}button:disabled{opacity:.6;cursor:wait}
.secret{margin-top:22px;padding:18px;border:1px solid #425681;border-radius:12px;background:#182540}.secret code{display:block;margin:12px 0;word-break:break-all;font-size:17px;color:#fff}
.error{margin-top:10px;color:#ff9e9e;font-size:14px}.note{margin:22px 0 0;font-size:13px}.note strong{color:#d8e0f4}
@media(max-width:560px){.header-inner{padding:16px}.header-inner .admin-badge{font-size:10px;padding:6px 9px}main{margin:24px auto;padding:0 16px}.panel{padding:24px 18px}.form-card{padding:17px}button{width:100%}}
</style></head><body><header><div class="header-inner"><div class="brand">Quizcord <span>Analytics</span></div><div class="admin-badge">ADMIN CONSOLE</div></div></header><main>
<a class="back" href="/">← <span>Back to admin dashboard</span></a><section class="panel"><div class="eyebrow">Administrator tools · Faculty accounts</div><h1>Faculty password management</h1>
<p class="description">Issue or reset a faculty member's app password. Enter their Discord user ID; a one-time password will be generated and shown only once. They must change it after signing in.</p>
<div class="form-card"><form id="provision-form"><label for="discord-id">Faculty member's Discord user ID</label><input id="discord-id" inputmode="numeric" pattern="[0-9]+" autocomplete="off" placeholder="Example: 123456789012345678" required><br><button id="submit" type="submit">Generate temporary password</button><div id="error" class="error" role="alert"></div></form></div>
<div id="secret" class="secret" hidden><strong>Copy this password and send it securely to the faculty member. It will not be shown again.</strong><code id="temporary-password"></code><button id="copy" type="button">Copy password</button></div>
<p class="note"><strong>Access requirement:</strong> The member must be in the configured server and have the configured Faculty role. Resetting a password invalidates the previous one.</p>
</section>
<script>
const form=document.getElementById("provision-form"),id=document.getElementById("discord-id"),error=document.getElementById("error"),secret=document.getElementById("secret"),temporary=document.getElementById("temporary-password"),submit=document.getElementById("submit");
form.addEventListener("submit",async event=>{event.preventDefault();error.textContent="";secret.hidden=true;submit.disabled=true;try{const response=await fetch("/api/admin/faculty-passwords/"+encodeURIComponent(id.value.trim()),{method:"POST",cache:"no-store"});const body=await response.json();if(response.status===401){location.replace("/");return;}if(!response.ok)throw new Error(body.detail||"Could not provision faculty password.");temporary.textContent=body.temporary_password;secret.hidden=false;}catch(exception){error.textContent=exception.message;}finally{submit.disabled=false;}});
document.getElementById("copy").addEventListener("click",async()=>{await navigator.clipboard.writeText(temporary.textContent);document.getElementById("copy").textContent="Copied";});
</script></main></body></html>"""

FACULTY_PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>Faculty · Quizcord Analytics</title>
  <style>
    :root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;color:#e7edf8;background:#0b1020}
    *{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(ellipse at 10% 0%,#17264a 0,transparent 42%),#0b1020}
    button,a,input{font:inherit}.top{height:72px;display:flex;align-items:center;justify-content:space-between;padding:0 max(24px,calc((100vw - 1100px)/2));border-bottom:1px solid #26314b;background:#0b1020cc;position:sticky;top:0}
    .brand{font-weight:750}.brand span{color:#8fa7ff}.account{display:flex;gap:16px;align-items:center;color:#aebbd2;font-size:14px}.account a{color:#aebbd2;text-decoration:none}
    main{max-width:1100px;margin:38px auto;padding:0 24px}.eyebrow{color:#91a6ff;font-size:12px;text-transform:uppercase;letter-spacing:1.4px;font-weight:700}h1{font-size:30px;margin:8px 0}.intro{color:#aebbd2;margin:0 0 26px}
    .panel{background:#11182b;border:1px solid #26334e;border-radius:16px;overflow:hidden;margin:18px 0}.panel-head{padding:19px 21px;border-bottom:1px solid #26314b}.panel h2{font-size:16px;margin:0}.panel-sub{font-size:12px;color:#8291ac;margin-top:5px}.table-wrap{overflow:auto;max-height:520px}table{border-collapse:collapse;width:100%;text-align:left}th,td{padding:13px 18px;border-bottom:1px solid #202b42;font-size:13px}th{color:#8797b3;font-size:11px;text-transform:uppercase;letter-spacing:.7px;position:sticky;top:0;background:#11182b}
    .usn{font-weight:700;letter-spacing:.2px}.student-link{color:#dce5f8;text-decoration:none}.student-link:hover{color:#91a6ff;text-decoration:underline}.muted{color:#91a0ba}.button{border:1px solid #425681;border-radius:8px;background:#26365b;color:#e7edf8;padding:8px 12px;cursor:pointer;font-size:12px;font-weight:650}.button:hover{background:#344875}.button.remove{background:#2c2030;border-color:#654052}.button:disabled{opacity:.55;cursor:wait}
    .search-tools{display:flex;gap:12px;padding:16px 18px}.search{flex:1;min-width:0;padding:11px 12px;background:#0b1020;color:#e7edf8;border:1px solid #33415e;border-radius:9px}.empty{color:#8797b3;text-align:center;padding:36px 20px;font-size:14px}.error{color:#ff9e9e}.notice{padding:12px 18px;color:#95a4bd;font-size:13px}.row-metrics{white-space:nowrap;color:#aebbd2}.candidate-warning{margin-top:5px;color:#ffbd59;font-size:11px;line-height:1.4;max-width:460px}
    @media(max-width:600px){.top{padding:0 16px}.account{gap:8px}main{margin:24px auto;padding:0 16px}.search-tools{flex-direction:column}th,td{padding:12px}}
  </style>
</head>
<body>
  <header class="top"><div class="brand">Quizcord <span>Faculty</span></div><div class="account"><span id="account-name"></span><a href="/auth/logout">Sign out</a></div></header>
  <main>
    <div class="eyebrow">Student mentoring</div><h1>Monitor your students</h1>
    <p class="intro">Add students by USN to follow their Quizcord attendance and performance.</p>
    <section class="panel"><div class="panel-head"><h2>Your students</h2><div class="panel-sub">Only you can manage your student list.</div></div><div id="assigned"><div class="empty">Loading students…</div></div></section>
    <section class="panel"><div class="panel-head"><h2>Add your students</h2><div class="panel-sub">Search by USN, then add the student to your list.</div></div>
      <div class="search-tools"><input class="search" id="student-search" type="search" placeholder="Search by USN" aria-label="Search students by USN"></div>
      <div id="candidates"><div class="empty">Enter a USN to find students.</div></div>
    </section>
  </main>
  <script>
    const el=(tag,className,text)=>{const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=text;return node;};
    const assigned=document.getElementById("assigned"),candidates=document.getElementById("candidates"),search=document.getElementById("student-search");
    let assignedUSNs=new Set(),searchTimer;
    async function api(path,options={}){
      const response=await fetch(path,{cache:"no-store",...options});
      if(response.status===401){location.replace("/faculty/login");throw new Error("Sign-in expired.");}
      return response;
    }
    async function loadAssigned(){
      try{
        const response=await api("/api/faculty/students");
        if(!response.ok)throw new Error("Could not load your students.");
        const students=await response.json();assignedUSNs=new Set(students.map(student=>student.usn.toLocaleLowerCase()));
        if(!students.length){assigned.replaceChildren(el("div","empty","You haven't added any students yet. Search for a USN below to get started."));return;}
        const wrap=el("div","table-wrap"),table=el("table"),thead=el("thead"),head=el("tr");
        ["Student","Quizzes attended","Attendance","Total points",""].forEach(label=>head.append(el("th","",label)));
        thead.append(head);table.append(thead);const body=el("tbody");
        students.forEach(student=>{const row=el("tr"),nameCell=el("td","");
          const link=el("a","student-link",student.student_name);link.href="/faculty/students/"+encodeURIComponent(student.usn);
          nameCell.append(link,el("div","usn muted",student.usn));
          const remove=el("button","button remove","Remove");remove.type="button";remove.addEventListener("click",()=>removeStudent(student.usn,remove));
          const removeCell=el("td","");removeCell.appendChild(remove);
          row.append(nameCell,el("td","row-metrics",student.quizzes_attended+" / "+student.completed_quizzes),el("td","row-metrics",Number(student.attendance_percentage).toFixed(1)+"%"),el("td","row-metrics",String(student.total_points)),removeCell);body.append(row);
        });
        table.append(body);wrap.append(table);assigned.replaceChildren(wrap);
      }catch(error){assigned.replaceChildren(el("div","empty error",error.message));}
    }
    async function loadCandidates(){
      const query=search.value.trim();
      try{
        const response=await api("/api/faculty/candidates?search="+encodeURIComponent(query));
        if(!response.ok)throw new Error("Could not search students.");
        const users=await response.json();
        if(!users.length){candidates.replaceChildren(el("div","empty","No students found for that USN."));return;}
        const wrap=el("div","table-wrap"),table=el("table"),thead=el("thead"),head=el("tr");
        ["Student","USN",""].forEach(label=>head.append(el("th","",label)));
        thead.append(head);table.append(thead);const body=el("tbody");
        users.forEach(user=>{const row=el("tr");
          const studentCell=el("td","");studentCell.append(el("div","",user.student_name));
          if(!user.name_matches_usn_format)studentCell.append(el("div","candidate-warning","Ask this student to update their Discord global display name to their USN format before adding them."));
          row.append(studentCell,el("td","usn",user.usn||"USN not available"));
          const button=el("button","button",user.assigned?"Added":user.can_assign?"+ Add":"USN required");button.type="button";button.disabled=user.assigned||!user.can_assign;
          if(user.can_assign&&!user.assigned)button.addEventListener("click",()=>addStudent(user.usn,button));
          const actionCell=el("td","");actionCell.appendChild(button);row.append(actionCell);body.append(row);
        });
        table.append(body);wrap.append(table);candidates.replaceChildren(wrap);
      }catch(error){candidates.replaceChildren(el("div","empty error",error.message));}
    }
    async function addStudent(usn,button){
      button.disabled=true;
      try{
        const response=await api("/api/faculty/students/"+encodeURIComponent(usn),{method:"POST"});
        if(response.status===409){await loadAssigned();await loadCandidates();return;}
        if(!response.ok)throw new Error("Could not add this student.");
        await loadAssigned();await loadCandidates();
      }catch(error){candidates.prepend(el("div","notice error",error.message));button.disabled=false;}
    }
    async function removeStudent(usn,button){
      button.disabled=true;
      try{
        const response=await api("/api/faculty/students/"+encodeURIComponent(usn),{method:"DELETE"});
        if(!response.ok)throw new Error("Could not remove this student.");
        await loadAssigned();await loadCandidates();
      }catch(error){assigned.prepend(el("div","notice error",error.message));button.disabled=false;}
    }
    search.addEventListener("input",()=>{clearTimeout(searchTimer);searchTimer=setTimeout(loadCandidates,200);});
    api("/api/me").then(response=>{if(!response.ok)throw new Error();return response.json();}).then(user=>{document.getElementById("account-name").textContent=user.username;}).catch(()=>location.replace("/faculty/login"));
    loadAssigned();loadCandidates();
  </script>
</body>
</html>
"""


def _dashboard_user(user: DashboardUser) -> dict[str, Any]:
    return {
        "account_name": user.account_name,
        "global_name": user.global_name,
        "server_nickname": user.server_nickname,
        "usn": user.usn,
        "student_name": user.student_name,
        "name_matches_usn_format": user.name_matches_usn_format,
    }


def create_dashboard_app(settings: Settings, database: Database) -> FastAPI:
    app = FastAPI(title="Quizcord Analytics", docs_url=None, redoc_url=None)
    if settings.dashboard_session_secret:
        app.add_middleware(
            SessionMiddleware,
            secret_key=settings.dashboard_session_secret,
            session_cookie="quizcord_dashboard",
            max_age=OAUTH_SESSION_TTL_SECONDS,
            same_site="lax",
            https_only=True,
        )
    oauth_sessions: dict[str, _OAuthSession] = {}
    rate_limit_windows: OrderedDict[
        tuple[str, str], deque[float]
    ] = OrderedDict()

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        path = request.url.path
        rules: list[tuple[str, int, int]] = []
        if path != "/health":
            rules.append(("all-requests", *DEFAULT_REQUEST_RATE_LIMIT))
        if path.startswith("/api/"):
            rules.append(("api", *DEFAULT_API_RATE_LIMIT))
        specific_limit = RATE_LIMIT_RULES.get(path)
        if specific_limit is None and path.startswith(
            "/api/admin/faculty-passwords/"
        ):
            specific_limit = RATE_LIMIT_RULES["/api/admin/faculty-passwords"]
        if specific_limit is not None:
            bucket_name = (
                "admin-password-provision"
                if path.startswith("/api/admin/")
                else path
            )
            rules.append((bucket_name, *specific_limit))

        if rules:
            client_ip = request.headers.get("x-real-ip", "").strip()
            if not client_ip:
                client_ip = (
                    request.client.host
                    if request.client is not None
                    else "unknown"
                )
            now = time.monotonic()
            buckets: list[tuple[tuple[str, str], deque[float], int, int]] = []
            exceeded: tuple[deque[float], int, int] | None = None
            for bucket_name, limit, window_seconds in rules:
                key = (client_ip, bucket_name)
                attempts = rate_limit_windows.get(key)
                if attempts is None:
                    attempts = deque()
                else:
                    rate_limit_windows.move_to_end(key)
                cutoff = now - window_seconds
                while attempts and attempts[0] <= cutoff:
                    attempts.popleft()
                buckets.append((key, attempts, limit, window_seconds))
                if exceeded is None and len(attempts) >= limit:
                    exceeded = (attempts, limit, window_seconds)

            if exceeded is not None:
                attempts, limit, window_seconds = exceeded
                retry_after = max(
                    1, int(attempts[0] + window_seconds - now + 0.999)
                )
                headers = {
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int(time.time()) + retry_after),
                }
                if path.startswith("/api/"):
                    response: Response = JSONResponse(
                        {
                            "detail": (
                                "Too many requests. Please wait before trying again."
                            )
                        },
                        status_code=429,
                        headers=headers,
                    )
                else:
                    headers["Content-Type"] = "text/html; charset=utf-8"
                    response = HTMLResponse(
                        _error_page(
                            429,
                            "Please wait before trying this sign-in action again.",
                        ),
                        status_code=429,
                        headers=headers,
                    )
            else:
                header_bucket = min(
                    buckets,
                    key=lambda bucket: bucket[2] / bucket[3],
                )
                for key, attempts, _, _ in buckets:
                    if key not in rate_limit_windows:
                        if len(rate_limit_windows) >= RATE_LIMIT_MAX_BUCKETS:
                            rate_limit_windows.popitem(last=False)
                        rate_limit_windows[key] = attempts
                    attempts.append(now)
                response = await call_next(request)
                key, attempts, header_limit, header_window = header_bucket
                reset_after = max(
                    1, int(attempts[0] + header_window - now + 0.999)
                )
                response.headers["X-RateLimit-Limit"] = str(header_limit)
                response.headers["X-RateLimit-Remaining"] = str(
                    max(0, header_limit - len(attempts))
                )
                response.headers["X-RateLimit-Reset"] = str(
                    int(time.time()) + reset_after
                )
        else:
            response = await call_next(request)

        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline' https://challenges.cloudflare.com; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self' https://challenges.cloudflare.com; "
            "frame-src https://challenges.cloudflare.com; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        request: Request, error: StarletteHTTPException
    ) -> Response:
        if _wants_json_error(request):
            return JSONResponse(
                {"detail": error.detail},
                status_code=error.status_code,
                headers=error.headers,
            )
        return HTMLResponse(
            _error_page(error.status_code, error.detail),
            status_code=error.status_code,
            headers=error.headers,
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(
        request: Request, error: Exception
    ) -> Response:
        LOGGER.exception("Unhandled dashboard request failure.")
        detail = "An unexpected server error occurred. Please try again."
        if _wants_json_error(request):
            return JSONResponse({"detail": detail}, status_code=500)
        return HTMLResponse(_error_page(500, detail), status_code=500)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    def require_oauth_config() -> tuple[str, str, str]:
        if (
            settings.dashboard_public_url is None
            or settings.discord_oauth_client_id is None
            or settings.discord_oauth_client_secret is None
        ):
            raise HTTPException(
                status_code=503,
                detail="The web dashboard is not configured.",
            )
        return (
            settings.dashboard_public_url,
            settings.discord_oauth_client_id,
            settings.discord_oauth_client_secret,
        )

    async def current_authenticated(request: Request) -> _OAuthSession:
        require_oauth_config()
        session_key = request.session.get("dashboard_session")
        auth_session = oauth_sessions.get(session_key) if session_key else None
        if auth_session is None or auth_session.expires_at <= time.time():
            request.session.clear()
            raise HTTPException(status_code=401, detail="Please sign in again.")

        async with auth_session.permission_check_lock:
            now = time.time()
            if now - auth_session.last_permission_check >= PERMISSION_CACHE_SECONDS:
                auth_session = await _verify_current_guild_access(
                    auth_session, settings
                )
                oauth_sessions[session_key] = auth_session
        if not auth_session.is_admin and not auth_session.is_faculty:
            raise HTTPException(
                status_code=403,
                detail="Administrator or Faculty role access to the configured server is required.",
            )
        return auth_session

    async def current_admin(request: Request) -> _OAuthSession:
        auth_session = await current_authenticated(request)
        if not auth_session.is_admin:
            raise HTTPException(
                status_code=403,
                detail="Administrator access to the configured server is required.",
            )
        return auth_session

    async def current_faculty(request: Request) -> _OAuthSession:
        if settings.faculty_role_id is None:
            raise HTTPException(
                status_code=503,
                detail="Faculty dashboard access is not configured.",
            )
        auth_session = await current_authenticated(request)
        if not auth_session.is_faculty and not auth_session.is_admin:
            raise HTTPException(
                status_code=403,
                detail="The configured Faculty role is required.",
            )
        if auth_session.is_faculty and (
            not auth_session.faculty_password_verified
            or auth_session.must_change_faculty_password
        ):
            raise HTTPException(
                status_code=428,
                detail="Complete faculty password verification before continuing.",
            )
        return auth_session

    @app.get("/auth/login")
    async def login(
        request: Request,
        session: str | None = None,
        destination: str | None = None,
    ) -> RedirectResponse:
        public_url, client_id, _ = require_oauth_config()
        now = time.time()
        expired_sessions = [
            key
            for key, auth_session in oauth_sessions.items()
            if auth_session.expires_at <= now
        ]
        for key in expired_sessions:
            del oauth_sessions[key]
        state = secrets.token_urlsafe(32)
        request.session.clear()
        request.session["oauth_state"] = state
        if destination == "faculty":
            request.session["return_to"] = "/faculty/password"
        elif session and len(session) == 32 and all(
            character in "0123456789abcdef" for character in session.casefold()
        ):
            request.session["return_to"] = f"/?session={session}"
        params = {
            "client_id": client_id,
            "redirect_uri": f"{public_url}/auth/callback",
            "response_type": "code",
            "scope": "identify guilds",
            "state": state,
        }
        return RedirectResponse(
            f"https://discord.com/oauth2/authorize?{urlencode(params)}",
            status_code=302,
        )

    @app.get("/auth/callback")
    async def callback(
        request: Request, code: str | None = None, state: str | None = None
    ) -> RedirectResponse:
        public_url, client_id, client_secret = require_oauth_config()
        expected_state = request.session.pop("oauth_state", None)
        if (
            not code
            or not state
            or not expected_state
            or not secrets.compare_digest(state, expected_state)
        ):
            request.session.clear()
            raise HTTPException(status_code=400, detail="Invalid sign-in state.")

        redirect_uri = f"{public_url}/auth/callback"
        oauth_stage = "authorization-code exchange"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                token_response = await client.post(
                    f"{DISCORD_API}/oauth2/token",
                    data={
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": redirect_uri,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                token_response.raise_for_status()
                token = token_response.json()
                access_token = token["access_token"]
                oauth_stage = "Discord identity lookup"
                user_response = await client.get(
                    f"{DISCORD_API}/users/@me",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                user_response.raise_for_status()
                user = user_response.json()
                oauth_stage = "Discord server-membership lookup"
                guilds_response = await client.get(
                    f"{DISCORD_API}/users/@me/guilds",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                guilds_response.raise_for_status()
                guilds = guilds_response.json()
                member = None
                if settings.faculty_role_id is not None and _is_guild_member(
                    guilds, settings.guild_id
                ):
                    oauth_stage = "Faculty role lookup"
                    member = await _get_guild_member(
                        client, settings, int(user["id"])
                    )
        except (httpx.HTTPError, KeyError, ValueError) as error:
            status_code = (
                error.response.status_code
                if isinstance(error, httpx.HTTPStatusError)
                else None
            )
            LOGGER.exception(
                "Discord OAuth failed during %s (HTTP %s).",
                oauth_stage,
                status_code or "transport/response parsing",
            )
            raise HTTPException(
                status_code=502,
                detail=_oauth_failure_detail(oauth_stage, status_code),
            ) from None

        is_admin = _is_guild_admin(guilds, settings.guild_id)
        is_faculty = _is_guild_faculty(member, settings.faculty_role_id)
        if not is_admin and not is_faculty:
            await _revoke_oauth_token(
                access_token, client_id, client_secret
            )
            raise HTTPException(
                status_code=403,
                detail="This Discord account is not an administrator and does not have the Faculty role in the configured server.",
            )
        return_to = request.session.get("return_to", "/")
        session_key = secrets.token_urlsafe(32)
        now = time.time()
        oauth_sessions[session_key] = _OAuthSession(
            user_id=int(user["id"]),
            username=str(user.get("global_name") or user.get("username") or "Admin"),
            access_token=access_token,
            refresh_token=str(token.get("refresh_token") or ""),
            access_token_expires_at=now + int(token.get("expires_in", 3600)),
            expires_at=now + OAUTH_SESSION_TTL_SECONDS,
            last_permission_check=now,
            is_admin=is_admin,
            is_faculty=is_faculty,
            faculty_password_verified=False,
            must_change_faculty_password=False,
            permission_check_lock=asyncio.Lock(),
        )
        request.session.clear()
        request.session["dashboard_session"] = session_key
        return RedirectResponse(return_to, status_code=302)

    @app.get("/auth/logout")
    async def logout(request: Request) -> RedirectResponse:
        _, client_id, client_secret = require_oauth_config()
        session_key = request.session.pop("dashboard_session", None)
        if session_key:
            auth_session = oauth_sessions.pop(session_key, None)
            if auth_session:
                await _revoke_oauth_token(
                    auth_session.access_token, client_id, client_secret
                )
        request.session.clear()
        return RedirectResponse("/", status_code=302)

    @app.get("/", response_class=HTMLResponse, response_model=None)
    async def index(request: Request) -> HTMLResponse | RedirectResponse:
        require_oauth_config()
        try:
            await current_admin(request)
        except HTTPException as error:
            if error.status_code == 401:
                requested_session = request.query_params.get("session", "")
                suffix = (
                    f"?session={requested_session}"
                    if len(requested_session) == 32
                    and all(
                        character in "0123456789abcdef"
                        for character in requested_session.casefold()
                    )
                    else ""
                )
                return RedirectResponse(f"/login{suffix}", status_code=302)
            raise
        return HTMLResponse(HTML_PAGE)

    @app.get("/faculty", response_class=HTMLResponse, response_model=None)
    async def faculty_page(request: Request) -> HTMLResponse | RedirectResponse:
        require_oauth_config()
        try:
            await current_faculty(request)
        except HTTPException as error:
            if error.status_code == 401:
                return RedirectResponse("/faculty/login", status_code=302)
            if error.status_code == 428:
                return RedirectResponse("/faculty/password", status_code=302)
            raise
        return HTMLResponse(FACULTY_PAGE)

    @app.get("/faculty/login", response_class=HTMLResponse)
    async def faculty_login_page() -> HTMLResponse:
        require_oauth_config()
        return HTMLResponse(FACULTY_LOGIN_PAGE)

    @app.get(
        "/faculty/password",
        response_class=HTMLResponse,
        response_model=None,
    )
    async def faculty_password_page(request: Request) -> HTMLResponse | RedirectResponse:
        try:
            auth_session = await current_authenticated(request)
        except HTTPException as error:
            if error.status_code == 401:
                return RedirectResponse("/faculty/login", status_code=302)
            raise
        if not auth_session.is_faculty and not auth_session.is_admin:
            raise HTTPException(
                status_code=403, detail="The configured Faculty role is required."
            )
        if auth_session.is_admin and not auth_session.is_faculty:
            return RedirectResponse("/faculty", status_code=302)
        page = FACULTY_PASSWORD_PAGE.replace(
            "__TURNSTILE_SITE_KEY__",
            html.escape(settings.turnstile_site_key or "", quote=True),
        )
        return HTMLResponse(page)

    @app.get(
        "/admin/faculty-passwords",
        response_class=HTMLResponse,
        response_model=None,
    )
    async def faculty_password_admin_page(
        request: Request,
    ) -> HTMLResponse | RedirectResponse:
        try:
            await current_admin(request)
        except HTTPException as error:
            if error.status_code == 401:
                return RedirectResponse("/login", status_code=302)
            raise
        return HTMLResponse(FACULTY_ADMIN_PAGE)

    @app.get(
        "/students/{usn}",
        response_class=HTMLResponse,
        response_model=None,
    )
    async def student_page(
        usn: str, request: Request
    ) -> HTMLResponse | RedirectResponse:
        try:
            await current_admin(request)
        except HTTPException as error:
            if error.status_code == 401:
                return RedirectResponse("/login", status_code=302)
            raise
        if not usn.strip() or len(usn) > 100:
            raise HTTPException(status_code=404, detail="Student not found.")
        return HTMLResponse(HTML_PAGE)

    @app.get("/faculty/students/{usn}", response_class=HTMLResponse)
    async def faculty_student_page(usn: str, request: Request) -> HTMLResponse:
        auth_session = await current_faculty(request)
        if not usn.strip() or len(usn) > 100:
            raise HTTPException(status_code=404, detail="Student not found.")
        if usn.casefold() not in {
            assigned.casefold()
            for assigned in database.get_faculty_student_usns(
                settings.guild_id, auth_session.user_id
            )
        }:
            raise HTTPException(status_code=404, detail="Student not found.")
        return HTMLResponse(HTML_PAGE)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(
        session: str | None = None, destination: str | None = None
    ) -> HTMLResponse:
        require_oauth_config()
        query = "?destination=faculty" if destination == "faculty" else ""
        if not query and session and len(session) == 32 and all(
            character in "0123456789abcdef" for character in session.casefold()
        ):
            query = f"?session={session}"
        return HTMLResponse(
            """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>Admin sign in · Quizcord Analytics</title>
  <style>
    :root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;color:#172033;background:#f4f6fb;font-synthesis:none}
    *{box-sizing:border-box}
    body{margin:0;min-height:100vh;display:grid;grid-template-columns:minmax(360px,1fr) minmax(420px,1fr);background:#f4f6fb}
    .brand-panel{position:relative;isolation:isolate;overflow:hidden;display:flex;flex-direction:column;justify-content:space-between;min-height:100vh;padding:42px clamp(32px,7vw,100px);color:#f6f8ff;background:linear-gradient(145deg,#152348 0%,#101a34 48%,#101629 100%)}
    .brand-panel::before,.brand-panel::after{position:absolute;z-index:-1;content:"";border:1px solid #9bb3ff20;border-radius:50%;pointer-events:none}
    .brand-panel::before{width:520px;height:520px;right:-260px;top:14%;box-shadow:0 0 0 50px #9bb3ff08,0 0 0 100px #9bb3ff06}
    .brand-panel::after{width:340px;height:340px;left:-230px;bottom:-140px;box-shadow:0 0 0 40px #9bb3ff08}
    .brand{display:flex;align-items:center;gap:12px;font-size:15px;font-weight:800;letter-spacing:.08em;text-transform:uppercase}
    .brand-mark{display:grid;place-items:center;width:38px;height:38px;border:1px solid #96aaff66;border-radius:11px;background:#758bff1c;color:#bdcaff;font-size:16px}
    .brand-copy{max-width:520px;margin:12vh 0 10vh}
    .kicker{display:flex;align-items:center;gap:10px;color:#aebfff;font-size:11px;font-weight:800;letter-spacing:.18em;text-transform:uppercase}
    .kicker::before{width:24px;height:2px;background:#8ea4ff;content:""}
    h1{max-width:500px;margin:20px 0 16px;font-size:clamp(38px,5vw,62px);line-height:1.04;letter-spacing:-.055em}
    .brand-copy p{max-width:430px;margin:0;color:#b8c3dc;font-size:16px;line-height:1.75}
    .preview{max-width:480px;padding:20px;border:1px solid #9aafff33;border-radius:16px;background:#ffffff0a;backdrop-filter:blur(10px)}
    .preview-heading{display:flex;align-items:center;justify-content:space-between;color:#e7ecff;font-size:12px;font-weight:700}
    .live-dot{display:inline-flex;align-items:center;gap:7px;color:#c4d0eb;font-size:11px;font-weight:500}
    .live-dot::before{width:7px;height:7px;border-radius:50%;background:#73d7b0;box-shadow:0 0 12px #73d7b088;content:""}
    .preview-bars{display:grid;grid-template-columns:repeat(12,1fr);align-items:end;gap:8px;height:74px;margin-top:18px}
    .preview-bars i{display:block;min-height:14px;border-radius:4px 4px 1px 1px;background:linear-gradient(180deg,#a9b8ff,#697ff0);opacity:.9}
    .preview-bars i:nth-child(3n){opacity:.48}.preview-bars i:nth-child(4n){background:linear-gradient(180deg,#79d9c0,#4e9f9b)}
    .brand-footer{color:#8f9bb7;font-size:12px}
    .login-side{display:grid;place-items:center;padding:36px clamp(24px,6vw,84px)}
    .login-card{width:min(100%,430px)}
    .mobile-brand{display:none}
    .eyebrow{color:#6775b2;font-size:11px;font-weight:800;letter-spacing:.16em;text-transform:uppercase}
    h2{margin:13px 0 10px;color:#182238;font-size:32px;letter-spacing:-.04em}
    .intro{margin:0 0 30px;color:#667086;font-size:15px;line-height:1.65}
    .discord-button{display:flex;align-items:center;justify-content:center;gap:11px;width:100%;min-height:54px;padding:0 18px;border:0;border-radius:10px;background:#5865f2;color:#fff;text-decoration:none;font-size:15px;font-weight:750;box-shadow:0 8px 18px #5865f233;transition:transform .15s,background .15s,box-shadow .15s}
    .discord-button:hover{transform:translateY(-1px);background:#4b58e8;box-shadow:0 12px 24px #5865f244}
    .discord-icon{width:20px;height:20px;fill:currentColor}
    .secure-note{display:flex;align-items:flex-start;gap:11px;margin-top:20px;padding:14px;border:1px solid #e4e8f1;border-radius:10px;background:#fff;color:#687389;font-size:12px;line-height:1.55}
    .secure-icon{flex:none;color:#6477cf;font-size:16px}
    .divider{height:1px;margin:28px 0 20px;background:#e5e8ef}
    .faculty-prompt{margin:0;color:#737d91;font-size:13px}
    .faculty-prompt a{color:#4d5ebd;font-weight:700;text-decoration:none}
    .faculty-prompt a:hover{text-decoration:underline}
    @media(max-width:850px){body{grid-template-columns:1fr}.brand-panel{display:none}.login-side{min-height:100vh;padding:28px 22px}.mobile-brand{display:flex;align-items:center;gap:10px;margin-bottom:48px;color:#172033;font-size:13px;font-weight:800;letter-spacing:.08em;text-transform:uppercase}.mobile-brand .brand-mark{background:#e8ecff;color:#5367ca;border-color:#dce2ff}.login-card{width:min(100%,420px)}}
    @media(max-width:440px){.mobile-brand{margin-bottom:36px}h2{font-size:28px}.intro{font-size:14px}}
    @media(prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;transition:none!important}}
  </style>
</head>
<body>
  <section class="brand-panel" aria-label="Quizcord Analytics">
    <div class="brand"><span class="brand-mark" aria-hidden="true">Q</span><span>Quizcord Analytics</span></div>
    <div class="brand-copy">
      <div class="kicker">Administrator workspace</div>
      <h1>Make every quiz count.</h1>
      <p>Track participation, review student performance, and manage your server's quiz analytics from one place.</p>
      <div class="preview" aria-hidden="true">
        <div class="preview-heading"><span>Quiz activity</span><span class="live-dot">Analytics overview</span></div>
        <div class="preview-bars"><i style="height:38%"></i><i style="height:55%"></i><i style="height:43%"></i><i style="height:70%"></i><i style="height:51%"></i><i style="height:83%"></i><i style="height:62%"></i><i style="height:93%"></i><i style="height:68%"></i><i style="height:78%"></i><i style="height:59%"></i><i style="height:88%"></i></div>
      </div>
    </div>
    <div class="brand-footer">Secure access for your Discord server administrators.</div>
  </section>
  <main class="login-side">
    <section class="login-card">
      <div class="mobile-brand"><span class="brand-mark" aria-hidden="true">Q</span><span>Quizcord Analytics</span></div>
      <div class="eyebrow">Admin sign in</div>
      <h2>Welcome back</h2>
      <p class="intro">Sign in with Discord. Access is limited to administrators of the configured server.</p>
      <a class="discord-button" href="/auth/login">
        <svg class="discord-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M19.7 5.3a18.4 18.4 0 0 0-4.6-1.4l-.6 1.2a16.7 16.7 0 0 0-5 0l-.6-1.2a18.4 18.4 0 0 0-4.6 1.4C1.4 9.5.6 13.6 1 17.6a18.5 18.5 0 0 0 5.7 2.9l1.2-2a11.8 11.8 0 0 1-1.9-.9l.5-.4a13.1 13.1 0 0 0 11 0l.5.4a11.8 11.8 0 0 1-1.9.9l1.2 2a18.5 18.5 0 0 0 5.7-2.9c.5-4.7-.8-8.8-3.3-12.3ZM8.8 14.9c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2Zm6.4 0c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2Z"/></svg>
        Continue with Discord
      </a>
      <div class="secure-note"><span class="secure-icon" aria-hidden="true">◆</span><span>Access is verified through Discord. Only administrators of the configured server can open the admin dashboard.</span></div>
      <div class="divider"></div>
      <p class="faculty-prompt">Faculty member? <a href="/faculty/login">Go to the faculty portal</a></p>
    </section>
  </main>
</body>
</html>""".replace(
                'href="/auth/login"', f'href="/auth/login{query}"'
            )
        )

    @app.get("/api/me")
    async def me(request: Request) -> dict[str, str]:
        auth_session = await current_authenticated(request)
        return {
            "id": str(auth_session.user_id),
            "username": auth_session.username,
            "role": "admin" if auth_session.is_admin else "faculty",
        }

    @app.get("/api/sessions")
    async def sessions(request: Request) -> list[dict[str, Any]]:
        await current_admin(request)
        return [
            _summary_overview(summary)
            for summary in database.get_quiz_session_summaries(
                settings.guild_id, limit=50
            )
        ]

    @app.get("/api/users")
    async def users(request: Request) -> list[dict[str, Any]]:
        await current_admin(request)
        return [
            _dashboard_user(user)
            for user in database.get_dashboard_users(settings.guild_id)
        ]

    @app.get("/api/faculty/students")
    async def faculty_students(request: Request) -> list[dict[str, Any]]:
        auth_session = await current_faculty(request)
        assigned_usns = database.get_faculty_student_usns(
            settings.guild_id, auth_session.user_id
        )
        dashboard_users = {
            user.usn.casefold(): user
            for user in database.get_dashboard_users(settings.guild_id)
            if user.usn is not None
        }
        students = []
        for usn in assigned_usns:
            user = dashboard_users.get(usn.casefold())
            analytics = database.get_student_quiz_analytics(settings.guild_id, usn)
            students.append(
                {
                    "usn": usn,
                    "student_name": (
                        (
                            user.student_name
                            or user.global_name
                            or user.server_nickname
                        )
                        if user
                        else analytics.student_name
                    ),
                    "quizzes_attended": analytics.quizzes_attended,
                    "completed_quizzes": analytics.completed_quizzes,
                    "attendance_percentage": analytics.attendance_percentage,
                    "total_points": analytics.total_points,
                }
            )
        return students

    @app.post("/api/faculty/password/verify", response_model=None)
    async def verify_faculty_password(
        request: Request, payload: dict[str, str] = Body(...)
    ) -> dict[str, bool] | JSONResponse:
        auth_session = await current_authenticated(request)
        if not auth_session.is_faculty:
            raise HTTPException(
                status_code=403, detail="The configured Faculty role is required."
            )
        if not settings.turnstile_secret_key:
            raise HTTPException(
                status_code=503,
                detail="Security check is not configured. Contact an administrator.",
            )
        token = payload.get("turnstile_token", "")
        if not token or len(token) > 2048:
            return JSONResponse(
                status_code=428,
                content={
                    "detail": "Complete the security check before continuing.",
                    "captcha_required": True,
                },
            )
        if not await _verify_turnstile_token(settings, token):
            return JSONResponse(
                status_code=428,
                content={
                    "detail": "Security check expired or invalid. Please retry it.",
                    "captcha_required": True,
                },
            )
        password = payload.get("password", "")
        if not password or len(password) > 256:
            raise HTTPException(status_code=422, detail="Enter your faculty password.")
        valid, must_change = database.verify_faculty_password(
            settings.guild_id, auth_session.user_id, password
        )
        if not valid:
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "Faculty password is incorrect, not provisioned, or temporarily locked. Contact an administrator if you need a reset.",
                    "captcha_required": True,
                },
            )
        auth_session.faculty_password_verified = True
        auth_session.must_change_faculty_password = must_change
        return {"must_change": must_change}

    @app.post("/api/faculty/password/change")
    async def change_faculty_password(
        request: Request, payload: dict[str, str] = Body(...)
    ) -> dict[str, bool]:
        auth_session = await current_authenticated(request)
        if not auth_session.is_faculty:
            raise HTTPException(
                status_code=403, detail="The configured Faculty role is required."
            )
        current_password = payload.get("current_password", "")
        new_password = payload.get("new_password", "")
        if len(new_password) < 12 or len(new_password) > 128:
            raise HTTPException(
                status_code=422,
                detail="Your new faculty password must be between 12 and 128 characters.",
            )
        valid, _ = database.verify_faculty_password(
            settings.guild_id, auth_session.user_id, current_password
        )
        if not valid:
            raise HTTPException(
                status_code=401, detail="The current faculty password is incorrect."
            )
        if secrets.compare_digest(current_password, new_password):
            raise HTTPException(
                status_code=422,
                detail="Choose a new password different from the temporary password.",
            )
        database.change_faculty_password(
            settings.guild_id, auth_session.user_id, new_password
        )
        auth_session.faculty_password_verified = True
        auth_session.must_change_faculty_password = False
        return {"changed": True}

    @app.post("/api/admin/faculty-passwords/{discord_id}")
    async def provision_faculty_password(
        discord_id: int, request: Request
    ) -> dict[str, str]:
        await current_admin(request)
        if settings.faculty_role_id is None:
            raise HTTPException(
                status_code=503,
                detail="Set FACULTY_ROLE_ID before provisioning faculty passwords.",
            )
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                member = await _get_guild_member(client, settings, discord_id)
        except httpx.HTTPError:
            LOGGER.exception("Could not verify faculty during password provisioning.")
            raise HTTPException(
                status_code=503,
                detail="Could not verify this Discord member. Please retry.",
            ) from None
        if not _is_guild_faculty(member, settings.faculty_role_id):
            raise HTTPException(
                status_code=404,
                detail="No current member with the configured Faculty role was found for that Discord ID.",
            )
        temporary_password = secrets.token_urlsafe(18)
        database.set_faculty_temporary_password(
            settings.guild_id, discord_id, temporary_password
        )
        return {
            "discord_id": str(discord_id),
            "temporary_password": temporary_password,
        }

    @app.get("/api/faculty/candidates")
    async def faculty_candidates(
        request: Request, search: str = ""
    ) -> list[dict[str, Any]]:
        auth_session = await current_faculty(request)
        query = search.strip().upper()
        if len(query) > 100:
            raise HTTPException(status_code=422, detail="USN search is too long.")
        assigned_usns = {
            usn.casefold()
            for usn in database.get_faculty_student_usns(
                settings.guild_id, auth_session.user_id
            )
        }
        candidates = []
        for user in database.get_dashboard_users(settings.guild_id):
            if query and (
                user.usn is None or query not in user.usn.upper()
            ):
                continue
            candidates.append(
                {
                    "usn": user.usn,
                    "student_name": (
                        user.student_name
                        or user.global_name
                        or user.server_nickname
                        or user.usn
                        or user.account_name
                    ),
                    "can_assign": (
                        user.usn is not None
                        and matches_usn_format(user.global_name or "")
                        and user.usn.casefold()
                        == (user.global_name or "").strip().casefold()
                    ),
                    "name_matches_usn_format": user.name_matches_usn_format,
                    "assigned": (
                        user.usn.casefold() in assigned_usns
                        if user.usn is not None
                        else False
                    ),
                }
            )
        return candidates

    @app.post("/api/faculty/students/{usn}", status_code=204)
    async def add_faculty_student(
        usn: str, request: Request
    ) -> Response:
        auth_session = await current_faculty(request)
        if not usn.strip() or len(usn) > 100:
            raise HTTPException(status_code=404, detail="Student not found.")
        matching_user = next(
            (
                user
                for user in database.get_dashboard_users(settings.guild_id)
                if user.usn is not None
                and user.usn.casefold() == usn.casefold()
            ),
            None,
        )
        if matching_user is None:
            raise HTTPException(
                status_code=404,
                detail="No current server member has this USN.",
            )
        if (
            not matches_usn_format(matching_user.global_name or "")
            or usn.casefold()
            != (matching_user.global_name or "").strip().casefold()
        ):
            raise HTTPException(
                status_code=422,
                detail="This student cannot be added until their Discord global display name matches their USN format. Ask them to update it and try again.",
            )
        added = database.add_faculty_student(
            settings.guild_id, auth_session.user_id, usn
        )
        if not added:
            raise HTTPException(
                status_code=409, detail="This student is already in your list."
            )
        return Response(status_code=204)

    @app.delete("/api/faculty/students/{usn}", status_code=204)
    async def remove_faculty_student(
        usn: str, request: Request
    ) -> Response:
        auth_session = await current_faculty(request)
        if not database.remove_faculty_student(
            settings.guild_id, auth_session.user_id, usn
        ):
            raise HTTPException(status_code=404, detail="Student not found.")
        return Response(status_code=204)

    @app.get("/api/faculty/students/{usn}")
    async def faculty_student_analytics(
        usn: str, request: Request
    ) -> dict[str, Any]:
        auth_session = await current_faculty(request)
        if not usn.strip() or len(usn) > 100 or usn.casefold() not in {
            assigned.casefold()
            for assigned in database.get_faculty_student_usns(
                settings.guild_id, auth_session.user_id
            )
        }:
            raise HTTPException(status_code=404, detail="Student not found.")
        return _student_analytics(
            database.get_student_quiz_analytics(settings.guild_id, usn)
        )

    @app.get("/api/students/{usn}")
    async def student_analytics(usn: str, request: Request) -> dict[str, Any]:
        await current_admin(request)
        if not usn.strip() or len(usn) > 100:
            raise HTTPException(status_code=404, detail="Student not found.")
        return _student_analytics(
            database.get_student_quiz_analytics(settings.guild_id, usn)
        )

    @app.get("/api/students/{usn}/export.xlsx")
    async def export_student_analytics(usn: str, request: Request) -> Response:
        await current_admin(request)
        if not usn.strip() or len(usn) > 100:
            raise HTTPException(status_code=404, detail="Student not found.")
        analytics = database.get_student_quiz_analytics(settings.guild_id, usn)
        safe_usn = "".join(
            character
            for character in analytics.usn
            if character.isascii() and (character.isalnum() or character in "-_")
        )[:80]
        return Response(
            content=student_analytics_workbook(analytics),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="quizcord-student-{safe_usn or "report"}.xlsx"'
                )
            },
        )

    @app.get("/api/sessions/{session_id}")
    async def session_detail(
        session_id: str, request: Request
    ) -> dict[str, Any]:
        await current_admin(request)
        summary = database.get_quiz_session(session_id)
        if summary is None or summary.guild_id != settings.guild_id:
            raise HTTPException(status_code=404, detail="Quiz session not found.")
        return _summary_detail(database.get_quiz_session_summary(session_id))

    @app.get("/api/sessions/{session_id}/export.xlsx")
    async def export_session(session_id: str, request: Request) -> Response:
        await current_admin(request)
        session = database.get_quiz_session(session_id)
        if session is None or session.guild_id != settings.guild_id:
            raise HTTPException(status_code=404, detail="Quiz session not found.")
        summary = database.get_quiz_session_summary(session_id)
        if summary.session.status != "completed":
            raise HTTPException(
                status_code=409,
                detail="The quiz export is available after the quiz completes.",
            )
        quiz_id = summary.session.quizcord_quiz_id or summary.session.session_id
        safe_quiz_id = "".join(
            character for character in quiz_id if character.isalnum() or character in "-_"
        )[:80]
        return Response(
            content=quiz_session_workbook(summary),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f'attachment; filename="quizcord-{safe_quiz_id}.xlsx"'
            },
        )

    return app


async def _verify_current_guild_access(
    auth_session: _OAuthSession, settings: Settings
) -> _OAuthSession:
    now = time.time()
    if auth_session.access_token_expires_at <= now:
        if not auth_session.refresh_token:
            raise HTTPException(status_code=401, detail="Please sign in again.")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    f"{DISCORD_API}/oauth2/token",
                    data={
                        "client_id": settings.discord_oauth_client_id,
                        "client_secret": settings.discord_oauth_client_secret,
                        "grant_type": "refresh_token",
                        "refresh_token": auth_session.refresh_token,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                response.raise_for_status()
                token = response.json()
        except (httpx.HTTPError, ValueError, KeyError):
            LOGGER.exception("Could not refresh a dashboard OAuth session.")
            raise HTTPException(
                status_code=401, detail="Please sign in again."
            ) from None
        auth_session.access_token = token["access_token"]
        auth_session.refresh_token = str(
            token.get("refresh_token") or auth_session.refresh_token
        )
        auth_session.access_token_expires_at = now + int(
            token.get("expires_in", 3600)
        )

    member: Any = None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{DISCORD_API}/users/@me/guilds",
                headers={"Authorization": f"Bearer {auth_session.access_token}"},
            )
            response.raise_for_status()
            guilds = response.json()
            auth_session.is_admin = _is_guild_admin(
                guilds, settings.guild_id
            )
            if settings.faculty_role_id is not None and not auth_session.is_admin:
                member = await _get_guild_member(
                    client, settings, auth_session.user_id
                )
    except (httpx.HTTPError, ValueError):
        LOGGER.exception("Could not verify dashboard server roles.")
        raise HTTPException(
            status_code=503,
            detail="Dashboard server access could not be verified. Please retry.",
        ) from None

    auth_session.is_faculty = _is_guild_faculty(
        member, settings.faculty_role_id
    )
    auth_session.last_permission_check = now
    return auth_session


def _is_guild_admin(guilds: Any, guild_id: int) -> bool:
    if not isinstance(guilds, list):
        return False
    for guild in guilds:
        if not isinstance(guild, dict) or str(guild.get("id")) != str(guild_id):
            continue
        if guild.get("owner") is True:
            return True
        try:
            permissions = int(guild.get("permissions", "0"))
        except (TypeError, ValueError):
            return False
        return bool(
            permissions & (MANAGE_GUILD_PERMISSION | ADMINISTRATOR_PERMISSION)
        )
    return False


def _is_guild_faculty(member: Any, faculty_role_id: int | None) -> bool:
    if faculty_role_id is None or not isinstance(member, dict):
        return False
    roles = member.get("roles")
    if not isinstance(roles, list):
        return False
    return str(faculty_role_id) in {str(role_id) for role_id in roles}


async def _verify_turnstile_token(settings: Settings, token: str) -> bool:
    hostname = urlsplit(settings.dashboard_public_url or "").hostname
    if not settings.turnstile_secret_key or not hostname:
        raise HTTPException(
            status_code=503,
            detail="Security check is not configured. Contact an administrator.",
        )
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={
                    "secret": settings.turnstile_secret_key,
                    "response": token,
                },
            )
            response.raise_for_status()
            result = response.json()
    except (httpx.HTTPError, ValueError):
        LOGGER.error("Cloudflare Turnstile verification request failed.")
        raise HTTPException(
            status_code=503,
            detail="Security check is temporarily unavailable. Please try again.",
        ) from None
    return (
        isinstance(result, dict)
        and result.get("success") is True
        and result.get("hostname") == hostname
        and result.get("action") == "faculty_password"
    )


async def _get_guild_member(
    client: httpx.AsyncClient, settings: Settings, user_id: int
) -> dict[str, Any] | None:
    response = await client.get(
        f"{DISCORD_API}/guilds/{settings.guild_id}/members/{user_id}",
        headers={"Authorization": f"Bot {settings.token}"},
    )
    if response.status_code == 404:
        return None
    if response.status_code == 401:
        LOGGER.error("Discord rejected the bot token during faculty role lookup.")
        raise HTTPException(
            status_code=503,
            detail="Faculty role verification failed because Discord rejected the bot token. Check DISCORD_BOT_TOKEN in the service configuration.",
        )
    if response.status_code == 403:
        LOGGER.error("The bot cannot access the configured server member endpoint.")
        raise HTTPException(
            status_code=503,
            detail="Faculty role verification failed because the bot cannot access the configured server. Confirm the bot is still a member of that server.",
        )
    response.raise_for_status()
    member = response.json()
    if not isinstance(member, dict):
        raise ValueError("Discord returned an invalid guild member response.")
    return member


def _is_guild_member(guilds: Any, guild_id: int) -> bool:
    return isinstance(guilds, list) and any(
        isinstance(guild, dict) and str(guild.get("id")) == str(guild_id)
        for guild in guilds
    )


def _oauth_failure_detail(stage: str, status_code: int | None) -> str:
    status = f" (HTTP {status_code})" if status_code is not None else ""
    if stage == "authorization-code exchange":
        return (
            "Discord rejected the OAuth authorization code"
            f"{status}. Check that DASHBOARD_PUBLIC_URL matches the Discord "
            "OAuth redirect URL exactly, including /auth/callback, and that "
            "the OAuth client ID and secret belong to that same Discord application. "
            "Restart sign-in to use a fresh authorization code."
        )
    if stage == "Faculty role lookup":
        return (
            "Discord could not verify the Faculty role"
            f"{status}. Confirm the bot can access the configured server and "
            "try signing in again."
        )
    return (
        f"Discord sign-in failed during {stage}{status}. "
        "Check the application logs and try again."
    )


async def _revoke_oauth_token(
    access_token: str, client_id: str, client_secret: str
) -> None:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(
                f"{DISCORD_API}/oauth2/token/revoke",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "token": access_token,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()
    except httpx.HTTPError:
        LOGGER.warning("Could not revoke a dashboard OAuth token.")


def _summary_overview(summary: QuizSessionSummary) -> dict[str, Any]:
    session = summary.session
    return {
        "session_id": session.session_id,
        "status": session.status,
        "started_at": session.started_at,
        "completed_at": session.completed_at,
        "quizcord_quiz_id": session.quizcord_quiz_id,
        "question_count": summary.question_count,
        "participant_count": len(summary.participants),
        "attendance_count": summary.attendance_count,
        "eligible_student_count": session.eligible_student_count,
        "attendance_percentage": summary.attendance_percentage,
        "total_points": summary.total_points,
        "average_points": summary.average_points,
    }


def _summary_detail(summary: QuizSessionSummary) -> dict[str, Any]:
    session = summary.session
    return {
        "session": {
            "session_id": session.session_id,
            "status": session.status,
            "started_at": session.started_at,
            "completed_at": session.completed_at,
            "quizcord_quiz_id": session.quizcord_quiz_id,
        },
        "question_count": summary.question_count,
        "attendance_count": summary.attendance_count,
        "eligible_student_count": session.eligible_student_count,
        "attendance_percentage": summary.attendance_percentage,
        "total_points": summary.total_points,
        "average_points": summary.average_points,
        "participants": [
            {
                "usn": participant.usn,
                "rank": participant.rank,
                "points": participant.points,
                "name_matches_usn_format": participant.name_matches_usn_format,
            }
            for participant in summary.participants
        ],
    }


def _student_analytics(analytics: StudentQuizAnalytics) -> dict[str, Any]:
    return {
        "usn": analytics.usn,
        "student_name": analytics.student_name,
        "quizzes_attended": analytics.quizzes_attended,
        "completed_quizzes": analytics.completed_quizzes,
        "attendance_percentage": analytics.attendance_percentage,
        "total_points": analytics.total_points,
        "average_points": analytics.average_points,
        "history": [
            {
                "quizcord_quiz_id": record.quizcord_quiz_id,
                "status": record.status,
                "started_at": record.started_at,
                "rank": record.rank,
                "points": record.points,
            }
            for record in analytics.history
        ],
    }
