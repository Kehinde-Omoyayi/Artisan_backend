(() => {
  // Renders "Oct 14, 2026 · 5:22 PM" in the viewer's own locale/time zone.
  const formatTimestamp = (rawValue) => {
    const date = new Date(rawValue || "");
    if (Number.isNaN(date.getTime())) return "";
    const parts = new Intl.DateTimeFormat(undefined, {
      month: "short",
      day: "numeric",
      year: "numeric",
      hour: "numeric",
      minute: "2-digit",
    }).formatToParts(date);
    const get = (type) => (parts.find((part) => part.type === type) || {}).value || "";
    const time = [get("hour"), get("minute")].filter(Boolean).join(":");
    const dayPeriod = get("dayPeriod");
    return `${get("month")} ${get("day")}, ${get("year")} · ${time}${dayPeriod ? ` ${dayPeriod}` : ""}`;
  };

  document.querySelectorAll(".inbox-card").forEach((card) => {
    const timestamp = card.querySelector("time[data-utc-time]");
    if (timestamp) {
      const formatted = formatTimestamp(timestamp.dataset.utcTime);
      if (formatted) timestamp.textContent = formatted;
    }

    card.addEventListener("click", () => {
      card.setAttribute("aria-busy", "true");
    });
  });
})();