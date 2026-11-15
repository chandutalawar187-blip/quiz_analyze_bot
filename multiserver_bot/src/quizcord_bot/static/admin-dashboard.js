const select = document.getElementById("session-select");
    const content = document.getElementById("content");
    const dashboardTabs = document.getElementById("dashboard-tabs");
    const sessionHeading = document.getElementById("session-heading");
    const newerSessions = document.getElementById("sessions-newer");
    const olderSessions = document.getElementById("sessions-older");
    const leaderboardSearch = document.getElementById("leaderboard-search");
    const initialSession = new URLSearchParams(location.search).get("session");
    const facultyStudentPath = location.pathname.startsWith("/faculty/students/");
    const studentPath = facultyStudentPath ? location.pathname.slice("/faculty/students/".length) : location.pathname.startsWith("/students/") ? location.pathname.slice("/students/".length) : null;
    const studentUSN = studentPath ? decodeURIComponent(studentPath) : null;
    const guildSelect = document.getElementById("guild-select");
    const safe = value => String(value ?? "");
    const el = (tag, className, text) => { const node=document.createElement(tag); if(className)node.className=className; if(text!==undefined)node.textContent=text; return node; };
    const icon = name => { const node=el("span","icon icon-"+name); node.setAttribute("aria-hidden","true"); return node; };
    const actionContent = (iconName,label) => { const content=el("span","action-content"); content.append(icon(iconName),document.createTextNode(label)); return content; };
    const getSavedGuild = () => { try { return localStorage.getItem("quizcord-selected-guild"); } catch { return null; } };
    const setSavedGuild = guildId => { try { localStorage.setItem("quizcord-selected-guild", guildId); } catch {} };
    function reloadForGuild(guildId){const url=new URL(location.href);url.searchParams.set("guild_id",guildId);url.searchParams.delete("session");location.assign(url);}
    async function initializeGuildSelector(){
      const response=await fetch("/api/guilds",{cache:"no-store"});
      if(!response.ok)throw new Error("Could not load your Discord servers.");
      const guilds=await response.json();
      const adminGuilds=guilds.filter(guild=>guild.role==="admin");
      if(!adminGuilds.length)throw new Error("The bot is not installed in a server you can manage.");
      guildSelect.replaceChildren();
      adminGuilds.forEach(guild=>{const option=document.createElement("option");option.value=guild.id;option.textContent=guild.name;guildSelect.append(option);});
      const saved=getSavedGuild();
      const requested=new URLSearchParams(location.search).get("guild_id");
      const preferred=requested && adminGuilds.some(guild=>guild.id===requested)?requested: saved && adminGuilds.some(guild=>guild.id===saved)?saved: adminGuilds[0].id;
      guildSelect.value=preferred;
      guildSelect.hidden=adminGuilds.length<2;
      const selected=await fetch("/api/guilds/select",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({guild_id:guildSelect.value}),cache:"no-store"});
      if(!selected.ok)throw new Error("Could not select the Discord server.");
      setSavedGuild(guildSelect.value);
      // Remove server and session identifiers from the URL before replay starts.
      if(location.pathname==="/"&&location.search)history.replaceState(history.state,"",location.pathname);
      guildSelect.addEventListener("change",async()=>{guildSelect.disabled=true;try{const changed=await fetch("/api/guilds/select",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({guild_id:guildSelect.value}),cache:"no-store"});if(!changed.ok)throw new Error("Could not select the Discord server.");setSavedGuild(guildSelect.value);reloadForGuild(guildSelect.value);}catch(error){content.replaceChildren(el("div","empty error",error.message));guildSelect.disabled=false;}});
    }
    let activeSession = initialSession;
    let sessionOffset = 0;
    let activeView = "sessions";
    let pollBusy = false;
    let userSearchQuery = "";
    let pendingDirectoryUsers = null;
    let leaderboardSearchQuery = "";
    let leaderboardRequestId = 0;
    let attendanceView = "attended";
    function filterLeaderboard(){
      const query=leaderboardSearchQuery.trim().toLocaleLowerCase();
      const rows=content.querySelectorAll(".leaderboard-row");
      let visibleCount=0;
      rows.forEach(row=>{
        row.hidden=!row.dataset.search.includes(query);
        if(!row.hidden)visibleCount++;
      });
      const emptyState=content.querySelector(".leaderboard-no-results");
      if(emptyState){
        emptyState.hidden=visibleCount>0;
        emptyState.querySelector("td").textContent=query
          ?"No students match your search."
          :attendanceView==="attended"
            ?"No students attended this quiz."
            :"No students are absent from this quiz.";
      }
    }
    function metric(label,value){
      const iconName=label.includes("Student")||label.includes("Quizzes attended")?"users":label.includes("rate")||label.includes("points")?"chart-bar":label.includes("updates")?"clock":"book-2";
      const labelNode=el("div","metric-label");labelNode.append(icon(iconName),document.createTextNode(label));
      const card=el("div","card");card.append(labelNode,el("div","metric",value));return card;
    }
    async function renderTopLeaderboard(){
      const requestId=++leaderboardRequestId;
      const heading=el("section","heading");
      const title=el("div");
      title.append(
        el("div","eyebrow","Server standings"),
        el("h1","","Top leaderboard"),
        el("p","top-leaderboard-description","Best performers by average normalized standing across completed quizzes.")
      );
      heading.append(title);

      const method=el("details","leaderboard-method");
      method.append(
        el("summary","","How rankings are calculated"),
        el("p","","Each completed quiz is normalized separately: (opponents scored below + half of tied opponents) ÷ all other eligible players. Percentiles are averaged equally across quizzes, so points and correct-answer counts are never added together. Bots, incomplete quizzes, single-player quizzes, and imports without a complete participant leaderboard are excluded.")
      );
      const skeleton=el("section","leaderboard-loading");
      skeleton.setAttribute("aria-label","Loading leaderboard");
      skeleton.setAttribute("aria-busy","true");
      const podiumSkeleton=el("div","leaderboard-podium leaderboard-podium-skeleton");
      for(let index=0;index<3;index++){
        const card=el("div","leaderboard-podium-card leaderboard-podium-placeholder");
        const rank=el("span","skeleton skeleton-rank");
        const avatar=el("span","skeleton skeleton-avatar");
        const name=el("span","skeleton skeleton-row-name");
        const score=el("span","skeleton skeleton-score");
        [rank,avatar,name,score].forEach(part=>part.setAttribute("aria-hidden","true"));
        card.append(rank,avatar,name,score);
        podiumSkeleton.append(card);
      }
      const listSkeleton=el("section","panel leaderboard-list-panel");
      const skeletonHeading=el("div","leaderboard-list-heading");
      skeletonHeading.append(el("strong","","Rankings"));
      listSkeleton.append(skeletonHeading);
      const skeletonRows=el("div","leaderboard-rows");
      for(let index=0;index<6;index++){
        const row=el("div","leaderboard-row leaderboard-row-placeholder");
        const rank=el("span","skeleton skeleton-rank");
        const avatar=el("span","skeleton skeleton-row-avatar");
        const name=el("span","skeleton skeleton-row-name");
        const score=el("span","skeleton skeleton-row-points");
        [rank,avatar,name,score].forEach(part=>part.setAttribute("aria-hidden","true"));
        row.append(rank,avatar,name,score);
        skeletonRows.append(row);
      }
      listSkeleton.append(skeletonRows);
      skeleton.append(podiumSkeleton,listSkeleton);
      content.replaceChildren(heading,method,skeleton);
      try{
        const response=await fetch("/api/top-leaderboard",{cache:"no-store"});
        if(response.status===401){location.replace("/");return;}
        if(!response.ok){
          const error=await response.json().catch(()=>({}));
          throw new Error(error.detail||"Could not load the top leaderboard.");
        }
        const data=await response.json();
        if(requestId!==leaderboardRequestId||activeView!=="top-leaderboard")return;
        let selectedPeriod="weekly";
        let searchTerm="";
        const controls=el("section","leaderboard-controls");
        const tabs=el("div","leaderboard-period-tabs");
        tabs.setAttribute("role","group");
        tabs.setAttribute("aria-label","Choose leaderboard period");
        const periodLabel=el("span","leaderboard-current-period");
        const search=document.createElement("input");
        search.className="leaderboard-search";
        search.type="search";
        search.placeholder="Search students";
        search.setAttribute("aria-label","Search leaderboard students");
        const searchField=el("label","leaderboard-search-field");
        searchField.append(icon("search"),search);
        const results=el("section","leaderboard-results");
        const periodButtons=[];
        [["weekly","This week"],["lifelong","All time"]].forEach(([key,label])=>{
          const button=el("button","leaderboard-period-tab",label);
          button.type="button";
          button.addEventListener("click",()=>{
            selectedPeriod=key;
            renderPeriod();
          });
          periodButtons.push([key,button]);
          tabs.append(button);
        });
        controls.append(tabs,periodLabel,searchField);

        const initials=name=>name.trim().split(/\s+/).slice(0,2).map(part=>part[0]).join("").toUpperCase()||"?";
        const profileLink=(entry,className)=>{
          if(!entry.usn)return el("span",className,entry.student_name);
          const link=document.createElement("a");
          link.className=className+" student-link";
          link.href="/students/"+encodeURIComponent(entry.usn);
          link.textContent=entry.student_name;
          return link;
        };
        const renderPeriod=()=>{
          const isWeekly=selectedPeriod==="weekly";
          const entries=Array.isArray(data[selectedPeriod])?data[selectedPeriod]:[];
          periodLabel.textContent=isWeekly?"Week of "+data.week_start+" · UTC":"All completed quizzes";
          periodButtons.forEach(([key,button])=>{
            const active=key===selectedPeriod;
            button.setAttribute("aria-pressed",String(active));
          });
          const podium=el("section","leaderboard-podium");
          podium.setAttribute("aria-label","Top three performers");
          if(entries.length){
            entries.slice(0,3).forEach(entry=>{
              const card=el("article","leaderboard-podium-card podium-rank-"+entry.rank);
              const badge=el("span","leaderboard-rank-badge","Rank #"+entry.rank);
              const avatar=el("span","leaderboard-avatar",initials(entry.student_name));
              avatar.setAttribute("aria-hidden","true");
              const identity=el("div","leaderboard-podium-identity");
              identity.append(profileLink(entry,"leaderboard-podium-name"));
              const score=Number(entry.average_percentile);
              card.append(
                badge,
                avatar,
                identity,
                el("strong","leaderboard-podium-score",score.toFixed(2)+"%"),
                el("span","leaderboard-podium-caption","average percentile")
              );
              podium.append(card);
            });
          }else{
            podium.append(el("div","empty leaderboard-empty","No ranked results for this period yet."));
          }

          const list=el("section","panel leaderboard-list-panel");
          const listHeading=el("div","leaderboard-list-heading");
          const listTitle=el("div");
          listTitle.append(
            el("h2","","All rankings"),
            el("span","panel-sub",entries.length+" students · normalized standing")
          );
          listHeading.append(listTitle,el("span","leaderboard-list-period",isWeekly?"Weekly":"Lifelong"));
          list.append(listHeading);
          const columns=el("div","leaderboard-columns");
          ["Rank","Student","Percentile","Quizzes"].forEach(label=>columns.append(el("span","",label)));
          list.append(columns);
          const rows=el("div","leaderboard-rows");
          entries.forEach(entry=>{
            const row=el("div","leaderboard-row");
            row.dataset.search=[entry.student_name,entry.usn,entry.rank].filter(Boolean).join(" ").toLocaleLowerCase();
            const rank=el("span","leaderboard-row-rank rank-"+entry.rank,"#"+entry.rank);
            const identity=el("div","leaderboard-row-identity");
            const avatar=el("span","leaderboard-row-avatar",initials(entry.student_name));
            avatar.setAttribute("aria-hidden","true");
            const student=el("div","leaderboard-row-student");
            student.append(profileLink(entry,"leaderboard-row-name"));
            if(entry.usn)student.append(el("span","leaderboard-row-usn",entry.usn));
            identity.append(avatar,student);
            const percentile=Math.min(100,Math.max(0,Number(entry.average_percentile)||0));
            const score=el("div","leaderboard-row-score");
            const bar=document.createElement("span");
            bar.style.width=percentile+"%";
            const track=el("span","leaderboard-progress");
            track.setAttribute("role","progressbar");
            track.setAttribute("aria-label",entry.student_name+" average percentile");
            track.setAttribute("aria-valuemin","0");
            track.setAttribute("aria-valuemax","100");
            track.setAttribute("aria-valuenow",String(percentile));
            track.append(bar);
            score.append(track,el("strong","",percentile.toFixed(2)+"%"));
            row.append(rank,identity,score,el("span","leaderboard-row-quizzes",String(entry.quizzes_ranked)));
            rows.append(row);
          });
          const noMatches=el("div","empty leaderboard-empty","No students match your search.");
          rows.append(noMatches);
          const filterRows=()=>{
            let visible=0;
            rows.querySelectorAll(".leaderboard-row").forEach(row=>{
              row.hidden=!row.dataset.search.includes(searchTerm);
              if(!row.hidden)visible++;
            });
            podium.hidden=Boolean(searchTerm);
            noMatches.hidden=visible>0||entries.length===0;
          };
          filterRows();
          search.oninput=()=>{
            searchTerm=search.value.trim().toLocaleLowerCase();
            filterRows();
          };
          list.append(rows);
          results.replaceChildren(podium,list);
        };
        renderPeriod();
        content.replaceChildren(heading,method,controls,results);
      }catch(error){
        if(requestId!==leaderboardRequestId||activeView!=="top-leaderboard")return;
        content.replaceChildren(heading,el("div","empty error",error.message));
      }
    }
    function statusLabel(status){return status==="completed"?"Complete":status==="interrupted"?"Interrupted":"Live";}
    function dateLabel(value,withTime=false){if(!value)return"Date unavailable";if(/^\d{4}-\d{2}-\d{2}$/.test(value)){const [year,month,day]=value.split("-").map(Number);return new Date(year,month-1,day).toLocaleDateString();}return new Date(value).toLocaleString(undefined,withTime?{}:{dateStyle:"short"});}
    function render(summary){
      const scrollTop=content.querySelector(".table-wrap")?.scrollTop??0;
      const session=summary.session;
      const complete=session.status==="completed";
      const participants=summary.participants||[];
      const absentStudents=summary.not_attended||[];
      const grid=el("section","grid");
      const statusCard=el("div","card status-card");
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
      const title=el("div");title.append(el("h2","",attendanceView==="attended"?"Students who attended":"Students who did not attend"),el("div","panel-sub",attendanceView==="attended"?(complete?"Final leaderboard snapshot":"Latest leaderboard snapshot"):"Based on the current synced human server roster"));
      const controls=el("div","attendance-controls");
      const toggle=el("button","attendance-toggle",attendanceView==="attended"?"Show not attended ("+absentStudents.length+")":"Show attended ("+participants.length+")");
      toggle.type="button";
      toggle.setAttribute("aria-pressed",String(attendanceView==="not-attended"));
      toggle.addEventListener("click",()=>{attendanceView=attendanceView==="attended"?"not-attended":"attended";render(summary);});
      controls.append(toggle,el("span","refresh","Updates automatically"));
      header.append(title,controls);roster.append(header);
      const wrap=el("div","table-wrap"),table=el("table"),thead=el("thead"),tr=el("tr");
      ["Rank","Student / USN","Quizcord points"].forEach(label=>tr.append(el("th","",label)));
      thead.append(tr);table.append(thead);const tbody=el("tbody");
      const noResults=el("tr","leaderboard-no-results");noResults.hidden=true;const message=el("td","empty");message.colSpan=3;noResults.append(message);
      if(attendanceView==="attended"){
        participants.forEach(person=>{const row=el("tr","leaderboard-row"),link=document.createElement("a");link.className="student-link";link.href="/students/"+encodeURIComponent(person.usn);link.textContent=person.usn;row.append(el("td","rank","#"+safe(person.rank)));const studentCell=el("td","usn");studentCell.append(link);if(person.name_matches_usn_format===false)studentCell.append(el("div","name-warning","Name not updated to USN"));row.append(studentCell,el("td","points",safe(person.points)));row.dataset.search=[person.name,person.usn,person.rank,person.points].filter(Boolean).join(" ").toLocaleLowerCase();tbody.append(row);});
      }else{
        absentStudents.forEach(student=>{const row=el("tr","leaderboard-row"),studentCell=el("td","usn"),label=student.usn||student.display_name;if(student.usn){const link=document.createElement("a");link.className="student-link";link.href="/students/"+encodeURIComponent(student.usn);link.textContent=student.display_name;studentCell.append(link);studentCell.append(el("div","panel-sub",student.usn));}else{studentCell.textContent=student.display_name;}row.append(el("td","rank","—"),studentCell,el("td","points","—"));row.dataset.search=[student.display_name,student.usn,label].filter(Boolean).join(" ").toLocaleLowerCase();tbody.append(row);});
      }
      tbody.append(noResults);table.append(tbody);wrap.append(table);roster.append(wrap);
      const side=el("article","panel");const sideHead=el("div","panel-head");sideHead.append(el("h2","","Student attendance"));side.append(sideHead);
      const ringWrap=el("div","ring-wrap"),ring=el("div","ring");
      ring.innerHTML='<svg viewBox="0 0 120 120" aria-hidden="true"><circle class="track" cx="60" cy="60" r="52"></circle><circle class="value" cx="60" cy="60" r="52"></circle></svg>';
      const ringValue=ring.querySelector(".value");
      if(ringValue&&attendanceRate!==null)ringValue.style.strokeDashoffset=String(326.7*(1-Math.min(100,attendanceRate)/100));
      const center=el("div","ring-center");center.append(el("div","ring-number",attendanceRate===null?"—":Number(attendanceRate).toFixed(1)+"%"),el("div","ring-caption",eligibleCount===null?"Member count unavailable":attendanceCount+" / "+eligibleCount+" attended"));ring.append(center);ringWrap.append(ring);side.append(ringWrap);
      const rows=el("div","summary-list");
      [["Human server members",eligibleCount??"Unavailable"],["Started",new Date(session.started_at).toLocaleString()]].forEach(([label,value])=>{const row=el("div","summary-row");row.append(el("span","",label),el("strong","",safe(value)));rows.append(row);});
      side.append(rows,el("div","note","Attendance includes human members regardless of server permissions. Bot accounts are excluded."));
      if(complete){const link=document.createElement("a");link.className="export-link";link.href="/api/sessions/"+encodeURIComponent(session.session_id)+"/export.xlsx";link.setAttribute("aria-label","Export Excel analytics for the selected quiz");link.append(actionContent("download","Export Excel Analytics"));link.style.display="flex";link.style.margin="0 20px 20px";side.append(link);}
      layout.append(roster,side);content.replaceChildren(grid,layout);filterLeaderboard();
      const tableWrap=content.querySelector(".table-wrap");
      if(tableWrap)tableWrap.scrollTop=scrollTop;
    }
    function renderUsers(users){
      const scrollTop=content.querySelector(".table-wrap")?.scrollTop??0;
      const heading=el("section","heading"),title=el("div","");
      title.append(el("div","eyebrow","Server directory"),el("h1","","All users"));
      heading.append(title,el("div","panel-sub",users.length+" human members"));
      const panel=el("article","panel"),tools=el("div","directory-tools");
      const search=document.createElement("input");search.className="directory-search";search.type="search";search.placeholder="Search names or USN";search.setAttribute("aria-label","Search users");
      search.value=userSearchQuery;
      const searchField=el("label","directory-search-field");searchField.append(icon("search"),search);
      tools.append(el("span","","Names marked below need a USN-format global display name."),searchField);panel.append(tools);
      const wrap=el("div","table-wrap"),table=el("table","directory-table"),thead=el("thead"),headRow=el("tr");
      ["Global display name","Account username","Server nickname","USN","Analytics"].forEach(label=>headRow.append(el("th","",label)));
      thead.append(headRow);table.append(thead);const body=el("tbody");
      const rows=[];
      users.forEach(user=>{const row=el("tr"),globalCell=el("td","");
        globalCell.append(el("span","",user.global_name||"Not set"));
        if(!user.name_matches_usn_format)globalCell.append(el("div","directory-warning","Name not updated to USN"));
        const analyticsCell=el("td","");
        if(user.usn){
          const analyticsLink=document.createElement("a");
          analyticsLink.className="directory-analytics-link";
          analyticsLink.href="/students/"+encodeURIComponent(user.usn);
          analyticsLink.textContent="View analytics";
          analyticsLink.setAttribute("aria-label","View analytics for "+user.usn);
          analyticsCell.append(analyticsLink);
        }else{
          analyticsCell.append(el("span","directory-analytics-unavailable","USN required"));
        }
        row.append(globalCell,el("td","",user.account_name),el("td","",user.server_nickname||"—"),el("td","usn",user.usn||"—"),analyticsCell);
        row.dataset.search=[user.global_name,user.account_name,user.server_nickname,user.student_name,user.usn].filter(Boolean).join(" ").toLocaleLowerCase();
        rows.push(row);body.append(row);
      });
      table.append(body);wrap.append(table);panel.append(wrap);
      if(!users.length)panel.append(el("div","empty","No synchronized human members found."));
      const filterRows=()=>{const query=search.value.trim().toLocaleLowerCase();rows.forEach(row=>{row.hidden=!row.dataset.search.includes(query);});};
      search.addEventListener("input",()=>{userSearchQuery=search.value;filterRows();});
      filterRows();
      content.replaceChildren(heading,panel);
      content.querySelector(".table-wrap").scrollTop=scrollTop;
    }
    function renderStudent(student){
      const scrollTop=content.querySelector(".table-wrap")?.scrollTop??0;
      document.getElementById("back-link").hidden=false;
      document.getElementById("back-link").href=facultyStudentPath?"/faculty":"/";
      const pageHeading=el("section","heading student-page-heading");
      const pageTitle=el("div","");
      pageTitle.append(el("div","eyebrow","Student analytics"),el("h1","",student.student_name));
      pageHeading.append(pageTitle);
      const heading=el("div","profile-heading");
      const headingDetails=el("div","");
      headingDetails.append(el("div","eyebrow",student.usn),el("div","panel-sub","Quiz attendance and performance across completed quizzes tracked by this bot."));
      const exportLink=document.createElement("a");exportLink.className="export-link";exportLink.href="/api/students/"+encodeURIComponent(student.usn)+"/export.xlsx";exportLink.append(actionContent("download","Download Excel"));heading.append(headingDetails,exportLink);
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
      const sections=[pageHeading,heading,grid];
      if(student.leaderboard){
        const leaderboardPanel=el("article","panel student-ranking-panel");
        const leaderboardHead=el("div","panel-head");
        leaderboardHead.append(el("h2","","Leaderboard ranking"));
        leaderboardPanel.append(leaderboardHead);
        const rankingGrid=el("div","student-ranking-grid");
        [
          ["Weekly rank",student.leaderboard.weekly,"Week of "+student.leaderboard.week_start+" UTC"],
          ["Lifelong rank",student.leaderboard.lifelong,"All time"]
        ].forEach(([label,ranking,period])=>{
          const card=el("div","student-ranking-card");
          card.append(
            el("div","student-ranking-label",label),
            el("div","student-ranking-value",ranking?"#"+ranking.rank:"Not ranked"),
            el(
              "div",
              "student-ranking-detail",
              ranking
                ?Number(ranking.average_percentile).toFixed(2)+"% average percentile · "+ranking.quizzes_ranked+" quizzes ranked"
                :"No qualifying completed quizzes"
            ),
            el("div","student-ranking-period",period)
          );
          rankingGrid.append(card);
        });
        leaderboardPanel.append(rankingGrid);
        sections.push(leaderboardPanel);
      }
      sections.push(profile,historyPanel);
      content.replaceChildren(...sections);
      const tableWrap=content.querySelector(".table-wrap");
      if(tableWrap)tableWrap.scrollTop=scrollTop;
    }
    async function refresh(){
      if(pollBusy||activeView!=="sessions")return;pollBusy=true;
      try{
        const response=await fetch("/api/sessions?offset="+sessionOffset,{cache:"no-store"});
        if(response.status===401){location.replace("/");return;}
        if(!response.ok)throw new Error("Could not load quiz sessions.");
        const sessions=await response.json();
        newerSessions.disabled=sessionOffset===0;
        olderSessions.disabled=sessions.length<50;
        activeSession=activeSession||sessions[0]?.session_id;
        if(!activeSession){content.replaceChildren(el("div","empty","No quiz sessions have been recorded yet. Start a Quizcord quiz to see its live dashboard."));return;}
        const detail=await fetch("/api/sessions/"+encodeURIComponent(activeSession),{cache:"no-store"});
        if(!detail.ok)throw new Error("Could not load the selected quiz.");
        const summary=await detail.json();
        if(!sessions.some(session=>session.session_id===activeSession))sessions.push(summary.session);
        select.replaceChildren();
        sessions.forEach(session=>{const option=document.createElement("option");option.value=session.session_id;option.textContent=(session.quizcord_quiz_id||"Quiz session")+" · "+statusLabel(session.status)+" · "+new Date(session.started_at).toLocaleString();select.append(option);});
        select.value=activeSession;
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
        if(activeView==="users"){
          if(content.querySelector(".directory-search")===document.activeElement)pendingDirectoryUsers=users;
          else{pendingDirectoryUsers=null;renderUsers(users);}
        }
      }catch(error){content.replaceChildren(el("div","empty error",error.message));}
      finally{pollBusy=false;}
    }
    function switchView(view){
      activeView=view;
      dashboardTabs.querySelectorAll("button").forEach(button=>button.setAttribute("aria-selected",String(button.dataset.view===view)));
      sessionHeading.hidden=view!=="sessions";
      select.hidden=view!=="sessions";
      if(view==="users")refreshUsers();
      else if(view==="top-leaderboard")renderTopLeaderboard();
      else refresh();
    }
    dashboardTabs.addEventListener("click",event=>{const button=event.target.closest("button[data-view]");if(button)switchView(button.dataset.view);});
    leaderboardSearch.addEventListener("input",()=>{leaderboardSearchQuery=leaderboardSearch.value;filterLeaderboard();});
    select.addEventListener("change",()=>{activeSession=select.value;refresh();});
    function changeSessionPage(direction){if(pollBusy)return;sessionOffset=Math.max(0,sessionOffset+direction*50);activeSession=null;refresh();}
    newerSessions.addEventListener("click",()=>changeSessionPage(-1));
    olderSessions.addEventListener("click",()=>changeSessionPage(1));
    initializeGuildSelector().then(()=>{
    return fetch("/api/me").then(response=>{if(!response.ok)throw new Error("Sign-in expired.");return response.json();}).then(user=>{document.getElementById("account-name").textContent=user.username;document.getElementById("faculty-password-link").hidden=user.role!=="admin";});
    }).then(()=>{
    if(studentUSN){
      dashboardTabs.hidden=true;
      sessionHeading.hidden=true;
      const backLink=document.getElementById("back-link");
      backLink.hidden=false;
      backLink.href=facultyStudentPath?"/faculty":"/";
      async function refreshStudent(){try{const endpoint=facultyStudentPath?"/api/faculty/students/":"/api/students/";const response=await fetch(endpoint+encodeURIComponent(studentUSN),{cache:"no-store"});if(response.status===401){location.replace(facultyStudentPath?"/faculty/login":"/");return;}if(!response.ok)throw new Error("Could not load student analytics.");renderStudent(await response.json());}catch(error){content.replaceChildren(el("div","empty error",error.message));}}
      refreshStudent();setInterval(refreshStudent,10000);
    }else{refresh();setInterval(()=>{if(activeView==="users")refreshUsers();else refresh();},3000);}
    }).catch(error=>content.replaceChildren(el("div","empty error",error.message)));
