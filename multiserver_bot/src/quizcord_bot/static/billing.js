const guildSelect = document.querySelector("#guild-select");
const notice = document.querySelector("#notice");
const serverName = document.querySelector("#server-name");
const statusPill = document.querySelector("#status-pill");
const accessCopy = document.querySelector("#access-copy");
const periodValue = document.querySelector("#period-value");
const allocationValue = document.querySelector("#allocation-value");
const subscribeButton = document.querySelector("#subscribe-button");
const refreshButton = document.querySelector("#refresh-button");
const cancelButton = document.querySelector("#cancel-button");

let billing = null;
let busy = false;

function formatMoney(paise) {
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 2,
  }).format(paise / 100);
}

function formatDate(timestamp) {
  if (!Number.isInteger(timestamp)) return "—";
  return new Intl.DateTimeFormat("en-IN", {
    dateStyle: "medium",
    timeZone: "Asia/Kolkata",
  }).format(new Date(timestamp * 1000));
}

function showNotice(message, isError = false) {
  notice.textContent = message;
  notice.classList.toggle("error", isError);
  notice.hidden = !message;
}

async function requestJson(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { Accept: "application/json", ...options.headers },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.detail || "The request could not be completed.");
  }
  return payload;
}

function setBusy(nextBusy) {
  busy = nextBusy;
  subscribeButton.disabled = busy || !billing?.billing_configured;
  refreshButton.disabled = busy;
  cancelButton.disabled = busy;
}

function render(data) {
  billing = data;
  serverName.textContent = data.guild_name;
  allocationValue.textContent = formatMoney(data.developer_allocations_paise);

  const subscription = data.subscription;
  const graceIsActive = Number.isInteger(data.grace_expires_at)
    && data.grace_expires_at * 1000 > Date.now();
  const hasAccess = data.has_access;
  const label = !data.billing_configured
    ? "billing disabled"
    : subscription?.status || (graceIsActive ? "grace period" : "not subscribed");
  statusPill.textContent = label;
  statusPill.className = "status-pill";
  statusPill.classList.add(
    hasAccess ? (subscription ? "active" : "warning") : "inactive",
  );

  if (!data.billing_configured) {
    periodValue.textContent = "Not enabled";
  } else if (subscription?.current_end) {
    periodValue.textContent = `Paid through ${formatDate(subscription.current_end)}`;
  } else if (graceIsActive) {
    periodValue.textContent = `Grace through ${formatDate(data.grace_expires_at)}`;
  } else {
    periodValue.textContent = "No paid period recorded";
  }

  if (!data.billing_configured) {
    accessCopy.textContent = "Billing has not been enabled; dashboard access is unaffected.";
  } else if (subscription?.cancel_at_cycle_end) {
    accessCopy.textContent = `Renewal is cancelled. Dashboard access remains through ${formatDate(subscription.current_end)}.`;
  } else if (hasAccess && subscription) {
    accessCopy.textContent = "This server’s dashboard subscription is active. It renews automatically each month.";
  } else if (graceIsActive) {
    accessCopy.textContent = `This server is in its one-time transition grace period until ${formatDate(data.grace_expires_at)}.`;
  } else if (hasAccess) {
    accessCopy.textContent = "Dashboard access is available during the server’s transition grace period.";
  } else {
    accessCopy.textContent = "Dashboard access is paused until a subscription is active. Quiz collection by the bot continues.";
  }

  subscribeButton.textContent = subscription?.status === "created"
    ? "Continue checkout"
    : "Start subscription";
  const canStartSubscription = !subscription
    || ["completed", "expired"].includes(subscription.status)
    || (subscription.status === "cancelled" && !hasAccess);
  subscribeButton.hidden = Boolean(
    subscription && subscription.status !== "created" && !canStartSubscription,
  );
  cancelButton.hidden = !(
    subscription
    && ["active", "pending", "halted"].includes(subscription.status)
    && subscription.current_end
    && !subscription.cancel_at_cycle_end
  );
  if (!data.billing_configured) {
    showNotice(
      "Online billing is not configured yet. Contact the Quizcord Analytics developer; no payment will be attempted.",
    );
  } else {
    showNotice("");
  }
  setBusy(busy);
}

async function loadBilling() {
  try {
    const data = await requestJson("/api/billing");
    render(data);
    if (data.subscription) await refreshStatus(false);
  } catch (error) {
    showNotice(error.message, true);
    setBusy(false);
  }
}

async function refreshStatus(showSuccess = true) {
  setBusy(true);
  try {
    const data = await requestJson("/api/billing/refresh", { method: "POST" });
    render(data);
    if (showSuccess) showNotice("Subscription status refreshed.");
  } catch (error) {
    showNotice(error.message, true);
  } finally {
    setBusy(false);
  }
}

async function loadGuilds() {
  const [guilds, current] = await Promise.all([
    requestJson("/api/guilds"),
    requestJson("/api/me"),
  ]);
  const adminGuilds = guilds.filter((guild) => guild.role === "admin");
  guildSelect.replaceChildren();
  for (const guild of adminGuilds) {
    const option = document.createElement("option");
    option.value = guild.id;
    option.textContent = guild.name;
    guildSelect.append(option);
  }
  if (adminGuilds.length === 0) {
    throw new Error("Manage Server permission is required to manage billing.");
  }
  guildSelect.value = adminGuilds.some((guild) => guild.id === current.guild_id)
    ? current.guild_id
    : adminGuilds[0].id;
  if (guildSelect.value !== current.guild_id) {
    await requestJson("/api/guilds/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ guild_id: guildSelect.value }),
    });
  }
}

guildSelect.addEventListener("change", async () => {
  setBusy(true);
  try {
    await requestJson("/api/guilds/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ guild_id: guildSelect.value }),
    });
    await loadBilling();
  } catch (error) {
    showNotice(error.message, true);
  } finally {
    setBusy(false);
  }
});

subscribeButton.addEventListener("click", async () => {
  setBusy(true);
  try {
    const result = await requestJson("/api/billing/checkout", {
      method: "POST",
    });
    window.location.assign(result.checkout_url);
  } catch (error) {
    showNotice(error.message, true);
    setBusy(false);
  }
});

refreshButton.addEventListener("click", () => refreshStatus());

cancelButton.addEventListener("click", async () => {
  if (!window.confirm("Cancel automatic renewal at the end of the current paid period?")) {
    return;
  }
  setBusy(true);
  try {
    const data = await requestJson("/api/billing/cancel", { method: "POST" });
    render(data);
    showNotice("Automatic renewal has been cancelled at the end of this period.");
  } catch (error) {
    showNotice(error.message, true);
  } finally {
    setBusy(false);
  }
});

loadGuilds()
  .then(loadBilling)
  .catch((error) => {
    showNotice(error.message, true);
    setBusy(false);
  });
