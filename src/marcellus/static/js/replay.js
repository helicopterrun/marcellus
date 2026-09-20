// Push replay workbench. POST starts the run in the background; we poll
// /replay/status for progress so a 1x scenario doesn't hold a request open
// for its whole wall-clock duration.
(function () {
  var btn = document.getElementById("run-btn");
  var stateEl = document.getElementById("run-state");
  var output = document.getElementById("output");
  var POLL_MS = 1000;

  function renderRun(run) {
    stateEl.textContent =
      run.state + " (" + run.messages_sent + "/" + run.messages_total + ")";

    if (run.decisions && run.decisions.length > 0) {
      output.textContent = run.decisions
        .map(function (d) {
          var parts = [
            "step " + d.step + ": " + d.topic.split("/").pop() + "/" + d.type,
            "mutation=" + d.mutation,
          ];
          if (d.level) parts.push("level=" + d.level);
          if (d.sounded !== undefined)
            parts.push("sounded=" + (d.sounded ? "yes" : "no"));
          if (d.sound_name) parts.push("sound=" + d.sound_name);
          if (d.interruption_level)
            parts.push("interruption=" + d.interruption_level);
          if (d.la_action) {
            var la = "LA=" + d.la_action;
            if (d.la_token_type) la += " (" + d.la_token_type + ")";
            parts.push(la);
          }
          return parts.join("  ");
        })
        .join("\n");
    } else if (run.state === "done") {
      output.textContent = run.messages_sent + " messages published to MQTT";
    }

    if (run.error) {
      output.textContent += "\nerror: " + run.error;
    }
  }

  async function pollUntilDone(runId) {
    for (;;) {
      await new Promise(function (r) { setTimeout(r, POLL_MS); });
      var data;
      try {
        data = await SC.fetchJson("/replay/status");
      } catch (e) {
        continue; // transient — keep polling
      }
      var run = data.run;
      if (!run || run.run_id !== runId) return;
      renderRun(run);
      if (run.state === "done" || run.state === "failed") return;
    }
  }

  btn.addEventListener("click", async function () {
    btn.disabled = true;
    output.textContent = "";
    stateEl.textContent = "starting...";

    var scenario = document.getElementById("scenario").value;
    var speed = parseFloat(
      document.querySelector('input[name="speed"]:checked').value
    );
    var dryRun = document.getElementById("dry-run").checked;
    var stacked = document.getElementById("stacked").checked;

    var scenarios = [scenario];
    var stagger = 8;
    if (stacked) {
      scenarios.push(document.getElementById("scenario2").value);
      stagger = parseFloat(document.getElementById("stagger").value) || 8;
    }

    try {
      var run = await SC.fetchJson("/replay/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          scenarios: scenarios,
          speed: speed,
          dry_run: dryRun,
          stagger: stagger,
        }),
      });
      renderRun(run);
      if (run.state !== "done" && run.state !== "failed") {
        await pollUntilDone(run.run_id);
      }
    } catch (e) {
      stateEl.textContent = "error: " + e.message;
    }
    btn.disabled = false;
  });
})();
