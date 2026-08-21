// PDP Content Scoring intake — repeatable URL fields.
//
// Loaded as an external file because the app's CSP is script-src 'self', so
// inline scripts and inline event handlers are blocked. Progressive
// enhancement: without JS the first URL field and the CSV upload still work;
// this only adds the ability to enter several URLs at once.
(function () {
  "use strict";

  var rows = document.getElementById("url-rows");
  var addButton = document.getElementById("add-url");
  if (!rows || !addButton) {
    return;
  }

  var MAX_ROWS = 200; // Mirror of app.pdp.MAX_ITEMS; server enforces the real cap.

  // Build a fresh, empty URL row matching the server-rendered markup so a
  // no-JS submission and a JS-added row post identically (name="urls").
  function makeRow() {
    var row = document.createElement("div");
    row.className = "url-row";

    var input = document.createElement("input");
    input.type = "url";
    input.name = "urls";
    input.className = "url-input";
    input.placeholder = "https://www.walmart.com/ip/10294528 (use this format)";

    var remove = document.createElement("button");
    remove.type = "button";
    remove.className = "row-remove";
    remove.setAttribute("aria-label", "Remove this URL");
    remove.innerHTML = "&times;";

    row.appendChild(input);
    row.appendChild(remove);
    return row;
  }

  addButton.addEventListener("click", function () {
    if (rows.querySelectorAll(".url-row").length >= MAX_ROWS) {
      return;
    }
    var row = makeRow();
    rows.appendChild(row);
    var input = row.querySelector(".url-input");
    if (input) {
      input.focus();
    }
  });

  // One delegated handler for every current and future remove button. Always
  // leave at least one row so the form still has a URL field.
  rows.addEventListener("click", function (event) {
    var button = event.target.closest(".row-remove");
    if (!button) {
      return;
    }
    var allRows = rows.querySelectorAll(".url-row");
    if (allRows.length <= 1) {
      // Clear the sole remaining field rather than removing it.
      var lone = rows.querySelector(".url-input");
      if (lone) {
        lone.value = "";
        lone.focus();
      }
      return;
    }
    var row = button.closest(".url-row");
    if (row) {
      row.remove();
    }
  });
})();
