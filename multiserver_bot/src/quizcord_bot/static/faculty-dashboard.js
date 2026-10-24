const el=(tag,className,text)=>{const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=text;return node;};
    const icon=name=>{const node=el("span","icon icon-"+name);node.setAttribute("aria-hidden","true");return node;};
    const actionContent=(iconName,label)=>{const content=el("span","action-content");content.append(icon(iconName),document.createTextNode(label));return content;};
const getSavedGuild = () => { try { return localStorage.getItem("quizcord-selected-guild"); } catch { return null; } };
const setSavedGuild = guildId => { try { localStorage.setItem("quizcord-selected-guild", guildId); } catch {} };
const assigned=document.getElementById("assigned"),candidates=document.getElementById("candidates"),search=document.getElementById("student-search");
let assignedUSNs=new Set(),searchTimer;
    function replaceTable(container,tableWrap){
      const scrollTop=container.querySelector(".table-wrap")?.scrollTop??0;
      container.replaceChildren(tableWrap);
      const updatedTable=container.querySelector(".table-wrap");
      if(updatedTable)updatedTable.scrollTop=scrollTop;
    }
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
          const remove=el("button","button remove");remove.type="button";remove.append(actionContent("x","Remove"));remove.addEventListener("click",()=>removeStudent(student.usn,remove));
          const removeCell=el("td","");removeCell.appendChild(remove);
          row.append(nameCell,el("td","row-metrics",student.quizzes_attended+" / "+student.completed_quizzes),el("td","row-metrics",Number(student.attendance_percentage).toFixed(1)+"%"),el("td","row-metrics",String(student.total_points)),removeCell);body.append(row);
        });
        table.append(body);wrap.append(table);replaceTable(assigned,wrap);
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
          const button=el("button","button");button.type="button";button.disabled=user.assigned||!user.can_assign;
          button.append(actionContent(user.assigned?"check":user.can_assign?"user-plus":"user-plus",user.assigned?"Added":user.can_assign?"Add":"USN required"));
          if(user.can_assign&&!user.assigned)button.addEventListener("click",()=>addStudent(user.usn,button));
          const actionCell=el("td","");actionCell.appendChild(button);row.append(actionCell);body.append(row);
        });
        table.append(body);wrap.append(table);replaceTable(candidates,wrap);
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
    function reloadForGuild(guildId){const url=new URL(location.href);url.searchParams.set("guild_id",guildId);url.searchParams.delete("session");location.assign(url);}
    search.addEventListener("input",()=>{clearTimeout(searchTimer);searchTimer=setTimeout(loadCandidates,200);});
    async function initializeGuildSelector(){
      const selector=document.getElementById("guild-select");
      const response=await api("/api/guilds");
      if(!response.ok)throw new Error("Could not load your Discord servers.");
      const guilds=await response.json();
      const facultyGuilds=guilds.filter(guild=>guild.is_faculty);
      if(!facultyGuilds.length)throw new Error("The bot is not installed in a server you can access.");
      selector.replaceChildren();
      facultyGuilds.forEach(guild=>{const option=document.createElement("option");option.value=guild.id;option.textContent=guild.name;selector.append(option);});
      const currentResponse=await api("/api/me");
      const currentGuildId=currentResponse.ok? (await currentResponse.json()).guild_id : null;
      const saved=getSavedGuild();
      const requested=new URLSearchParams(location.search).get("guild_id");
      const isFacultyGuild=guildId=>facultyGuilds.some(guild=>guild.id===guildId);
      const preferred=requested && isFacultyGuild(requested)?requested: saved && isFacultyGuild(saved)?saved: currentGuildId && isFacultyGuild(currentGuildId)?currentGuildId: facultyGuilds[0].id;
      selector.value=preferred;
      selector.hidden=facultyGuilds.length<2;
      const selected=await api("/api/guilds/select",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({guild_id:selector.value})});
      if(!selected.ok)throw new Error("Could not select the Discord server.");
      setSavedGuild(selector.value);
      selector.addEventListener("change",async()=>{selector.disabled=true;try{const changed=await api("/api/guilds/select",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({guild_id:selector.value})});if(!changed.ok)throw new Error("Could not select the Discord server.");setSavedGuild(selector.value);reloadForGuild(selector.value);}catch(error){assigned.replaceChildren(el("div","empty error",error.message));selector.disabled=false;}});
    }
    initializeGuildSelector().then(()=>api("/api/me")).then(response=>{if(!response.ok)throw new Error("Sign-in expired.");return response.json();}).then(user=>{document.getElementById("account-name").textContent=user.username;loadAssigned();loadCandidates();}).catch(error=>assigned.replaceChildren(el("div","empty error",error.message)));
