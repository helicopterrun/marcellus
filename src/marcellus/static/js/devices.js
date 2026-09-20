// Test-push buttons on the /settings page (devices moved off /devices).
document.querySelectorAll("button[data-token]").forEach(function (btn) {
  btn.addEventListener("click", async function () {
    btn.disabled = true;
    var original = btn.textContent;
    btn.textContent = "…";
    try {
      var resp = await fetch(
        "/v1/push/devices/" + encodeURIComponent(btn.dataset.token) + "/test",
        { method: "POST" }
      );
      if (resp.ok) {
        btn.textContent = "Sent ✓";
      } else {
        var body = await resp.json().catch(function () { return {}; });
        var msg = (body.detail && body.detail.message) || resp.status;
        btn.textContent = "Failed";
        btn.title = msg;
      }
    } catch (e) {
      btn.textContent = "Failed";
      btn.title = String(e);
    }
    setTimeout(function () {
      btn.textContent = original;
      btn.disabled = false;
    }, 2500);
  });
});
