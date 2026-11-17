// Small global helpers. Keep JavaScript to a minimum: HTMX + Alpine do most work.

// Show a readable message when an HTMX request fails, instead of failing silently.
document.addEventListener("htmx:responseError", function (event) {
  var status = event.detail.xhr.status;
  console.error("HTMX request failed", status, event.detail.requestConfig.path);
  alert(status === 403 ? "You don't have permission to do that." : "Something went wrong (" + status + "). Please try again.");
});
