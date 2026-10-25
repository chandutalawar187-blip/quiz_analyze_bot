(() => {
  const body = document.body;
  const dsn = body.dataset.sessionReplayDsn;
  if (
    body.dataset.sessionReplayEnabled !== "true" ||
    location.pathname !== "/" ||
    !dsn
  ) {
    return;
  }

  const cookieName = "quizcord_analytics_consent";
  const cookieLifetimeSeconds = 60 * 60 * 24 * 180;
  const preferences = document.getElementById("cookie-preferences");
  const panel = document.getElementById("cookie-preferences-panel");
  const analyticsEnabled = document.getElementById("cookie-analytics-enabled");
  const save = document.getElementById("cookie-preferences-save");
  const reject = document.getElementById("cookie-preferences-reject");
  const message = document.getElementById("cookie-preferences-message");
  const status = document.getElementById("cookie-preferences-status");
  const sdkUrl =
    "https://browser.sentry-cdn.com/11.9.0/bundle.tracing.replay.min.js";
  const sdkIntegrity =
    "sha384-zYqfHw4dRfd9q2US/rvKBTjujKInadFZPKcy2snfUBxf8h33manb/r85y0QNFgwe";
  let sdkPromise;
  let initialized = false;
  let consentRevision = 0;

  function report(messageText) {
    message.textContent = messageText;
    message.hidden = !messageText;
  }

  function setStatus(messageText) {
    status.textContent = messageText;
    status.hidden = !messageText;
  }

  function readChoice() {
    try {
      const entry = document.cookie
        .split(";")
        .map((part) => part.trim())
        .find((part) => part.startsWith(`${cookieName}=`));
      const choice = entry?.slice(cookieName.length + 1);
      return choice === "accepted" || choice === "declined" ? choice : null;
    } catch (error) {
      console.warn("Could not read analytics cookie preference.", error);
      return null;
    }
  }

  function saveChoice(choice) {
    try {
      const secure = location.protocol === "https:" ? "; Secure" : "";
      document.cookie = `${cookieName}=${choice}; Path=/; Max-Age=${cookieLifetimeSeconds}; SameSite=Lax${secure}`;
      if (readChoice() !== choice) {
        report("Your browser could not save this choice. Optional analytics remains off.");
        return false;
      }
      consentRevision += 1;
      return true;
    } catch (error) {
      console.warn("Could not save analytics cookie preference.", error);
      report("Your browser could not save this choice. Optional analytics remains off.");
      return false;
    }
  }

  function loadSdk() {
    if (window.Sentry) {
      return Promise.resolve();
    }
    if (sdkPromise) {
      return sdkPromise;
    }
    sdkPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = sdkUrl;
      script.integrity = sdkIntegrity;
      script.crossOrigin = "anonymous";
      script.referrerPolicy = "no-referrer";
      script.onload = resolve;
      script.onerror = () => reject(new Error("Sentry SDK failed to load."));
      document.head.append(script);
    });
    return sdkPromise;
  }

  function scrubSpan(span) {
    if (typeof span.name === "string") {
      span.name = span.name
        .replace(/(\/api\/sessions\/)[^/?\s]+/g, "$1:session")
        .replace(/\?.*$/, "");
    }
    if (span.attributes && typeof span.attributes === "object") {
      for (const key of Object.keys(span.attributes)) {
        if (/url|query/i.test(key)) {
          delete span.attributes[key];
        }
      }
    }
    return span;
  }

  async function startAnalytics() {
    const startRevision = consentRevision;
    await loadSdk();
    if (
      readChoice() !== "accepted" ||
      startRevision !== consentRevision
    ) {
      return;
    }
    if (!window.Sentry) {
      throw new Error("Sentry SDK did not initialize.");
    }
    if (!initialized) {
      window.Sentry.init({
        dsn,
        defaultIntegrations: false,
        sendDefaultPii: false,
        dataCollection: {
          urlQueryParams: false,
        },
        tracesSampler: () => (readChoice() === "accepted" ? 0.1 : 0),
        replaysSessionSampleRate: 0,
        replaysOnErrorSampleRate: 0,
        integrations: [
          window.Sentry.browserTracingIntegration(),
          window.Sentry.replayIntegration({
            maskAllText: true,
            maskAllInputs: true,
            blockAllMedia: true,
            networkCaptureBodies: false,
            networkDetailAllowUrls: [],
            maxReplayDuration: 900000,
          }),
        ],
        beforeSendSpan: scrubSpan,
      });
      initialized = true;
    }
    const replay = window.Sentry.getReplay();
    if (!replay) {
      throw new Error("Sentry Replay integration is unavailable.");
    }
    replay.start();
  }

  function openPreferences() {
    analyticsEnabled.checked = readChoice() === "accepted";
    panel.hidden = false;
    report("");
    analyticsEnabled.focus();
  }

  async function savePreferences() {
    if (analyticsEnabled.checked) {
      if (!saveChoice("accepted")) {
        return;
      }
      save.disabled = true;
      reject.disabled = true;
      report("Enabling optional analytics…");
      try {
        await startAnalytics();
        panel.hidden = true;
        report("");
        preferences.focus();
      } catch (error) {
        console.error("Could not enable optional analytics.", error);
        setStatus("Optional analytics could not start. No interaction data is being recorded.");
        report("Optional analytics could not start. You can retry from Cookie settings.");
      } finally {
        save.disabled = false;
        reject.disabled = false;
      }
      return;
    }
    await disableAnalytics();
  }

  async function disableAnalytics() {
    if (!saveChoice("declined")) {
      return;
    }
    save.disabled = true;
    reject.disabled = true;
    try {
      const replay = initialized ? window.Sentry.getReplay() : undefined;
      if (replay) {
        await replay.stop({ flush: false });
      }
      setStatus("");
      panel.hidden = true;
      report("");
      preferences.focus();
    } catch (error) {
      console.error("Could not stop optional analytics.", error);
      report("Optional analytics was turned off, but the browser could not confirm recording stopped. Reload this page.");
    } finally {
      save.disabled = false;
      reject.disabled = false;
    }
  }

  preferences.hidden = false;
  preferences.addEventListener("click", openPreferences);
  save.addEventListener("click", savePreferences);
  reject.addEventListener("click", disableAnalytics);

  const choice = readChoice();
  if (choice === "accepted") {
    analyticsEnabled.checked = true;
    startAnalytics().catch((error) => {
      console.error("Could not resume optional analytics.", error);
      setStatus("Optional analytics could not start. Open Cookie settings to retry.");
    });
  } else if (choice !== "declined") {
    openPreferences();
  }
})();
