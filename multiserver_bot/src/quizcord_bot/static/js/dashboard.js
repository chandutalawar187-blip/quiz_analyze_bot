const select = document.getElementById("session-select");
    const content = document.getElementById("content");
    const dashboardTabs = document.getElementById("dashboard-tabs");
    const sessionHeading = document.getElementById("session-heading");
    const initialSession = new URLSearchParams(location.search).get("session");
    const facultyStudentPath = location.pathname.startsWith("/faculty/students/");
    const studentPath = facultyStudentPath ? location.pathname.slice("/faculty/students/".length) : location.pathname.startsWith("/students/") ? location.pathname.slice("/students/".length) : null;
    const studentUSN = studentPath ? decodeURIComponent(studentPath) : null;
    const guildSelect = document.getElementById("guild-select");
    const safe = value => String(value ?? "");
    const el = (tag, className, text) => { const node=document.createElement(tag); if(className)node.className=className; if(text!==undefined)node.textContent=text; return node; };
    async function initializeGuildSelector(){
      const response=await fetch("/api/guilds",{cache:"no-store"});
      if(!response.ok)throw new Error("Could not load your Discord servers.");
      const guilds=await response.json();
      if(!guilds.length)throw new Error("The bot is not installed in a server you can manage.");
      guildSelect.replaceChildren();
      guilds.forEach(guild=>{const option=document.createElement("option");option.value=guild.id;option.textContent=guild.name;guildSelect.append(option);});
      const saved=localStorage.getItem("quizcord-selected-guild");
      const requested=new URLSearchParams(location.search).get("guild_id");
      guildSelect.value=guilds.some(guild=>guild.id===requested)?requested:guilds.some(guild=>guild.id===saved)?saved:guilds[0].id;
      guildSelect.hidden=guilds.length<2;
      const selected=await fetch("/api/guilds/select",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({guild_id:guildSelect.value}),cache:"no-store"});
      if(!selected.ok)throw new Error("Could not select the Discord server.");
      localStorage.setItem("quizcord-selected-guild",guildSelect.value);
      guildSelect.addEventListener("change",async()=>{guildSelect.disabled=true;try{const changed=await fetch("/api/guilds/select",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({guild_id:guildSelect.value}),cache:"no-store"});if(!changed.ok)throw new Error("Could not select the Discord server.");localStorage.setItem("quizcord-selected-guild",guildSelect.value);location.reload();}catch(error){content.replaceChildren(el("div","empty error",error.message));guildSelect.disabled=false;}});
    }
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
    initializeGuildSelector().then(()=>{
    return fetch("/api/me").then(response=>{if(!response.ok)throw new Error("Sign-in expired.");return response.json();}).then(user=>{document.getElementById("account-name").textContent=user.username;document.getElementById("faculty-password-link").hidden=user.role!=="admin";});
    }).then(()=>{
    if(studentUSN){
      dashboardTabs.hidden=true;
      select.hidden=true;
      async function refreshStudent(){try{const endpoint=facultyStudentPath?"/api/faculty/students/":"/api/students/";const response=await fetch(endpoint+encodeURIComponent(studentUSN),{cache:"no-store"});if(response.status===401){location.replace(facultyStudentPath?"/faculty/login":"/");return;}if(!response.ok)throw new Error("Could not load student analytics.");renderStudent(await response.json());}catch(error){content.replaceChildren(el("div","empty error",error.message));}}
      refreshStudent();setInterval(refreshStudent,10000);
    }else{refresh();setInterval(()=>{if(activeView==="users")refreshUsers();else refresh();},3000);}
    }).catch(error=>content.replaceChildren(el("div","empty error",error.message)));
