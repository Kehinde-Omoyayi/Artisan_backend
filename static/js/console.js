/* Manager Console page behaviour: date-range affordances only.
   Every value on the page is rendered by Django from an API response, so this
   file never computes a metric, a price or an audience size. */
(function () {
  "use strict";

  function syncCustomRange() {
    var select = document.querySelector("[data-console-period]");
    var fields = document.querySelectorAll("[data-console-custom]");
    if (!select || !fields.length) { return; }
    var toggle = function () {
      var show = select.value === "custom";
      fields.forEach(function (field) { field.hidden = !show; });
    };
    select.addEventListener("change", toggle);
    toggle();
  }

  document.addEventListener("DOMContentLoaded", function () {
    syncCustomRange();
  });
})();
