// PDP Content Scoring intake — repeatable URL fields + CSV drag-and-drop.
//
// Loaded as an external file because the app's CSP is script-src 'self', so
// inline scripts and inline event handlers are blocked. Everything here is
// progressive enhancement: without JS the first URL field and click-to-browse
// CSV upload still work; this adds multiple URLs, drag-and-drop, and a filename
// readout.
(function () {
  "use strict";

  var MAX_ROWS = 200; // Mirror of app.pdp.MAX_ITEMS; the server enforces the real cap.
  var PLACEHOLDER = "https://www.walmart.com/ip/10294528 (use this format)";

  // --- Repeatable URL rows -------------------------------------------------

  var rows = document.getElementById("url-rows");
  var addButton = document.getElementById("add-url");

  // Build a fresh, empty URL row matching the server-rendered markup so a
  // no-JS submission and a JS-added row post identically (name="urls").
  function makeRow() {
    var row = document.createElement("div");
    row.className = "url-row";

    var field = document.createElement("span");
    field.className = "url-field";

    var icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.setAttribute("class", "url-icon");
    icon.setAttribute("viewBox", "0 0 16 16");
    icon.setAttribute("fill", "none");
    icon.setAttribute("stroke", "currentColor");
    icon.setAttribute("stroke-width", "1.4");
    icon.setAttribute("aria-hidden", "true");
    var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute(
      "d",
      "M6.5 9.5l3-3M7 4.5l.8-.8a2.5 2.5 0 013.5 3.5l-.8.8M9 11.5l-.8.8a2.5 2.5 0 01-3.5-3.5l.8-.8"
    );
    icon.appendChild(path);

    var input = document.createElement("input");
    input.type = "url";
    input.name = "urls";
    input.className = "url-input";
    input.placeholder = PLACEHOLDER;

    field.appendChild(icon);
    field.appendChild(input);

    var remove = document.createElement("button");
    remove.type = "button";
    remove.className = "row-remove";
    remove.setAttribute("aria-label", "Remove this URL");
    remove.innerHTML = "&times;";

    row.appendChild(field);
    row.appendChild(remove);
    return row;
  }

  if (rows && addButton) {
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
      if (rows.querySelectorAll(".url-row").length <= 1) {
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
  }

  // --- CSV drag-and-drop + filename readout --------------------------------

  var drop = document.getElementById("csv-drop");
  var fileInput = document.getElementById("csv");
  var fileName = document.getElementById("csv-filename");

  function showFileName() {
    if (!fileName) {
      return;
    }
    if (fileInput.files && fileInput.files.length) {
      fileName.textContent = fileInput.files[0].name;
      fileName.hidden = false;
    } else {
      fileName.hidden = true;
    }
  }

  if (drop && fileInput) {
    fileInput.addEventListener("change", showFileName);

    ["dragenter", "dragover"].forEach(function (type) {
      drop.addEventListener(type, function (event) {
        event.preventDefault();
        drop.classList.add("is-dragging");
      });
    });

    ["dragleave", "dragend", "drop"].forEach(function (type) {
      drop.addEventListener(type, function (event) {
        event.preventDefault();
        drop.classList.remove("is-dragging");
      });
    });

    drop.addEventListener("drop", function (event) {
      if (event.dataTransfer && event.dataTransfer.files.length) {
        // Assigning to .files lets the dropped file submit with the form as if
        // it had been chosen through the picker.
        fileInput.files = event.dataTransfer.files;
        showFileName();
      }
    });
  }
})();
