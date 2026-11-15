(() => {
  const completeSignIn = (event) => {
    if (event.origin && event.origin !== window.location.origin) return;
    const message = event.data;
    if (
      !message ||
      message.type !== "quizcord-oauth-complete" ||
      typeof message.returnTo !== "string"
    ) {
      return;
    }

    let destination;
    try {
      destination = new URL(message.returnTo, window.location.origin);
    } catch {
      return;
    }
    if (destination.origin !== window.location.origin) return;
    window.location.replace(destination.href);
  };

  if ("BroadcastChannel" in window) {
    const channel = new BroadcastChannel("quizcord-oauth");
    channel.addEventListener("message", completeSignIn);
  }
  window.addEventListener("message", completeSignIn);

  document.querySelectorAll('a[href^="/auth/login"]').forEach((link) => {
    link.addEventListener("click", (event) => {
      if (
        event.defaultPrevented ||
        event.button !== 0 ||
        event.metaKey ||
        event.ctrlKey ||
        event.shiftKey ||
        event.altKey
      ) {
        return;
      }

      event.preventDefault();
      const loginUrl = new URL(link.href, window.location.href);
      loginUrl.searchParams.set("popup", "true");
      const popup = window.open(
        loginUrl.href,
        "quizcord-discord-signin",
        "popup,width=520,height=720,resizable=yes,scrollbars=yes",
      );
      const note = document.createElement("p");
      note.className = "oauth-popup-note";
      note.setAttribute("role", "status");
      link.insertAdjacentElement("afterend", note);
      note.textContent = popup
        ? "Complete sign-in in the Discord window; this page will continue automatically."
        : "Your browser blocked the sign-in window. Allow pop-ups for this site and try again.";
    });
  });
})();
