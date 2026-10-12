// Seasonal Campaigns — new-campaign form: default the planning dates from the
// chosen season.
//
// The date math (launch = 30 days before the season's holiday, expiration = the
// day after, reminder = 3 working days before launch) lives in Python
// (app/seasonal/themes.py) and is serialized into the page as a slug -> dates
// lookup. This script only *applies* that lookup to the three date inputs:
//   - on load, when the fields are empty (a fresh form), for the selected season;
//   - whenever the user picks a different season.
//
// Progressive enhancement: with JS off the fields are simply blank and the user
// fills them in (the dates are optional planning metadata server-side).

(function () {
  "use strict";

  var DATE_FIELDS = ["launch_date", "expiration_date", "reminder_date"];

  function parseDefaults() {
    var tag = document.getElementById("seasonal-theme-defaults");
    if (!tag) {
      return null;
    }
    try {
      return JSON.parse(tag.textContent || "{}");
    } catch (e) {
      // A malformed blob should never silently wedge the form; leave the fields
      // blank (the user can still enter dates) and record why in the console.
      console.error("seasonal_home: could not parse theme date defaults", e);
      return null;
    }
  }

  function inputs() {
    return DATE_FIELDS.map(function (name) {
      return document.querySelector('input[name="' + name + '"]');
    });
  }

  // Fill the three date inputs from the defaults for `slug`. An unknown slug
  // (e.g. Custom) clears them, matching the server's blank defaults.
  function applyDefaults(defaults, slug) {
    var dates = defaults[slug] || {};
    inputs().forEach(function (input, i) {
      if (input) {
        input.value = dates[DATE_FIELDS[i]] || "";
      }
    });
  }

  function init() {
    var defaults = parseDefaults();
    var select = document.getElementById("theme");
    if (!defaults || !select) {
      return;
    }

    // On load, only default dates the user hasn't already got (a fresh form).
    // On a validation-error re-render the submitted dates are preserved as-is.
    var hasExisting = inputs().some(function (input) {
      return input && input.value;
    });
    if (!hasExisting) {
      applyDefaults(defaults, select.value);
    }

    // Switching season re-defaults the dates (the whole point of the feature);
    // any manual edits are intentionally replaced.
    select.addEventListener("change", function () {
      applyDefaults(defaults, select.value);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
