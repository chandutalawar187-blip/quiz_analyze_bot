(() => {
  const content = document.getElementById("student-content");
  const usn = decodeURIComponent(location.pathname.slice("/faculty/students/".length));
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  function metric(label, value) {
    const card = el("article", "card");
    card.append(el("div", "metric-label", label), el("div", "metric", value));
    return card;
  }

  function formatDate(value) {
    if (!value) return "Date unavailable";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "Date unavailable" : date.toLocaleString();
  }

  function render(student) {
    document.title = `${student.student_name} · Quizcord Faculty`;
    document.getElementById("student-name").textContent = student.student_name;
    document.getElementById("student-usn").textContent = student.usn;

    const metrics = el("section", "grid");
    metrics.append(
      metric("Quizzes attended", student.quizzes_attended),
      metric("Completed quizzes tracked", student.completed_quizzes),
      metric("Attendance", `${Number(student.attendance_percentage).toFixed(1)}%`),
      metric("Total Quizcord points", student.total_points),
      metric(
        "Average points per attended quiz",
        student.average_points === null
          ? "—"
          : Number(student.average_points).toFixed(1),
      ),
    );

    const historyPanel = el("article", "panel");
    const panelHeader = el("div", "panel-head");
    panelHeader.append(el("h2", "", "Quiz history"));
    historyPanel.append(panelHeader);
    if (!student.history.length) {
      historyPanel.append(el("div", "empty", "No quiz attendance has been recorded."));
    } else {
      const tableWrap = el("div", "table-wrap");
      const table = el("table");
      const head = el("thead");
      const headerRow = el("tr");
      ["Date", "Quiz ID", "Status", "Rank", "Points"].forEach((label) => {
        headerRow.append(el("th", "", label));
      });
      head.append(headerRow);
      table.append(head);

      const body = el("tbody");
      student.history.forEach((record) => {
        const row = el("tr");
        const status =
          record.status === "completed"
            ? "Complete"
            : record.status === "active"
              ? "Live"
              : record.status === "historical"
                ? "Historical import"
                : "Interrupted";
        row.append(
          el("td", "", formatDate(record.started_at)),
          el("td", "", record.quizcord_quiz_id || "Not provided"),
          el("td", "", status),
          el("td", "rank", record.rank ? `#${record.rank}` : "—"),
          el("td", "points", record.points ?? "—"),
        );
        body.append(row);
      });
      table.append(body);
      tableWrap.append(table);
      historyPanel.append(tableWrap);
    }

    content.replaceChildren(metrics, historyPanel);
  }

  async function refresh() {
    try {
      const response = await fetch(`/api/faculty/students/${encodeURIComponent(usn)}`, {
        cache: "no-store",
      });
      if (response.status === 401) {
        location.replace("/faculty/login");
        return;
      }
      if (response.status === 428) {
        location.replace("/faculty/password");
        return;
      }
      if (!response.ok) {
        throw new Error(
          response.status === 404
            ? "This student is not assigned to your faculty account."
            : "Could not load student analytics.",
        );
      }
      render(await response.json());
    } catch (error) {
      content.replaceChildren(el("div", "empty error", error.message));
    }
  }

  fetch("/api/me", { cache: "no-store" })
    .then((response) => {
      if (response.status === 401) {
        location.replace("/faculty/login");
        throw new Error("Sign-in expired.");
      }
      if (!response.ok) throw new Error("Could not verify faculty access.");
      return response.json();
    })
    .then((user) => {
      document.getElementById("account-name").textContent = user.username;
      refresh();
      window.setInterval(refresh, 10000);
    })
    .catch((error) => {
      content.replaceChildren(el("div", "empty error", error.message));
    });
})();
