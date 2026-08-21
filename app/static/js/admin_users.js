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
