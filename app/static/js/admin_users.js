// Confirm before deleting a user. Progressive enhancement only: the delete form
// still submits without JS, and the server enforces auth + CSRF + the self-delete
// guard regardless — this just prevents an accidental click from going through.
document.addEventListener("submit", function (event) {
  var form = event.target;
  if (!form.classList || !form.classList.contains("delete-user-form")) {
    return;
  }
  var email = form.getAttribute("data-email") || "this user";
  var ok = window.confirm(
    "Delete " + email + " and all their scored items? This can't be undone."
  );
  if (!ok) {
    event.preventDefault();
  }
});

// Grant-add forms: reveal the brand or item picker that matches the chosen
// scope, and hide/disable the other so only the relevant ref is submitted.
// Progressive enhancement — without JS both selects show and the server still
// reads only the field matching the scope_type, so submission stays correct.
function syncGrantScope(select) {
  var form = select.closest(".grant-add-form");
  if (!form) {
    return;
  }
  var scope = select.value;
  form.querySelectorAll(".grant-ref").forEach(function (ref) {
    var isActive =
      (scope === "brand" && ref.classList.contains("grant-ref-brand")) ||
      (scope === "item" && ref.classList.contains("grant-ref-item"));
    ref.hidden = !isActive;
    // Disabled controls aren't submitted, so a hidden picker never sends a value.
    ref.disabled = !isActive;
    ref.required = isActive;
  });
}

document.addEventListener("change", function (event) {
  var el = event.target;
  if (el.classList && el.classList.contains("grant-scope-select")) {
    syncGrantScope(el);
  }
});

// Set the initial visibility on load (scope defaults to "whole portfolio").
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll(".grant-scope-select").forEach(syncGrantScope);
});
