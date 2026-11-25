"""Shared HTML account dropdown for pages served by either Flask app."""


_CURRENT_ACCOUNT_WIDGET = r"""
<script data-gw-current-account-widget>
(function () {
  if (document.getElementById("gw-account-widget")) return;

  var style = document.createElement("style");
  style.textContent = [
    "#gw-account-widget{position:relative;display:inline-flex;align-items:center;margin-left:auto;z-index:2147483000;font:600 14px/1.4 Inter,Segoe UI,Arial,sans-serif;color:#14213d}",
    "#gw-account-widget .gw-account-toggle{display:inline-flex;align-items:center;gap:8px;max-width:min(38vw,320px);padding:9px 12px;border:1px solid #d7e3f2;border-radius:11px;background:#fff;color:#164b91;box-shadow:0 3px 12px rgba(15,48,88,.08);font:inherit;cursor:pointer;transition:background .18s ease,border-color .18s ease,box-shadow .18s ease}",
    "#gw-account-widget .gw-account-toggle:hover,#gw-account-widget .gw-account-toggle[aria-expanded=true]{background:#f0f7ff;border-color:#9fc4ef;box-shadow:0 5px 16px rgba(15,48,88,.13)}",
    "#gw-account-widget .gw-account-toggle:focus-visible,#gw-account-widget .gw-account-logout:focus-visible{outline:3px solid rgba(37,115,213,.38);outline-offset:2px}",
    "#gw-account-widget .gw-account-icon{width:24px;height:24px;flex:0 0 24px;display:grid;place-items:center;border-radius:50%;background:#e8f2ff;color:#1769e0}",
    "#gw-account-widget .gw-account-icon svg{width:16px;height:16px;fill:currentColor}",
    "#gw-account-widget .gw-account-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
    "#gw-account-widget .gw-account-arrow{font-size:10px;color:#52708f;transition:transform .18s ease}",
    "#gw-account-widget .gw-account-toggle[aria-expanded=true] .gw-account-arrow{transform:rotate(180deg)}",
    "#gw-account-widget .gw-account-menu{position:absolute;top:calc(100% + 9px);right:0;z-index:2147483001;width:min(300px,calc(100vw - 24px));padding:8px;border:1px solid #dce7f5;border-radius:14px;background:#fff;box-shadow:0 18px 44px rgba(15,39,75,.2);transform-origin:top right;transition:opacity .16s ease,transform .16s ease}",
    "#gw-account-widget .gw-account-menu[hidden]{display:none}",
    "#gw-account-widget .gw-account-menu.gw-account-opening{opacity:0;transform:translateY(-5px) scale(.98)}",
    "#gw-account-widget .gw-account-summary{padding:10px 11px 12px;border-bottom:1px solid #e8eef6;overflow:hidden}",
    "#gw-account-widget .gw-account-summary-name{display:block;color:#14213d;font-size:14px;font-weight:750;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
    "#gw-account-widget .gw-account-email{display:block;margin-top:3px;color:#64748b;font-size:12px;font-weight:500;overflow-wrap:anywhere}",
    "#gw-account-widget .gw-account-logout{width:100%;display:flex;align-items:center;gap:9px;margin-top:6px;padding:10px 11px;border:0;border-radius:9px;background:transparent;color:#b42335;text-align:left;font:600 14px/1.4 inherit;cursor:pointer;transition:background .16s ease}",
    "#gw-account-widget .gw-account-logout:hover{background:#fff1f2}",
    "#gw-account-widget .gw-account-error{margin:5px 10px 7px;color:#b42335;font-size:12px}",
    "#gw-account-widget .gw-account-guest{display:inline-flex;align-items:center;gap:10px}",
    "#gw-account-widget .gw-account-guest a{color:#1769e0;text-decoration:none;font-weight:700}",
    "#gw-account-widget .gw-account-guest a:hover{text-decoration:underline}",
    "#gw-account-widget .gw-account-guest .gw-account-register{color:#52657d;font-weight:600}",
    "#navLogoutBtn{display:none!important}",
    ".logged-user-name,.user-dropdown,.logout-link{display:none!important}",
    "@media(max-width:640px){#gw-account-widget{max-width:52vw}#gw-account-widget .gw-account-toggle{max-width:52vw;padding:8px 9px;gap:6px;font-size:12px}#gw-account-widget .gw-account-menu{right:0}}"
  ].join("");
  document.head.appendChild(style);

  var host = document.getElementById("navUserArea") ||
    document.getElementById("userAuthLinks") ||
    document.getElementById("navLogoutBtn");

  if (host && host.id === "navLogoutBtn") {
    host.parentNode.insertBefore(createWidget(), host);
    host.remove();
  } else if (host) {
    host.replaceChildren(createWidget());
    host.style.display = "inline-flex";
    host.style.alignItems = "center";
    host.style.position = "relative";
    host.style.zIndex = "2147483000";
  } else {
    host = document.querySelector(".nav-right") ||
      document.querySelector(".nav-buttons") ||
      document.querySelector(".header-buttons") ||
      document.querySelector(".top-user-bar > div:last-child") ||
      document.querySelector(".user-bar") ||
      document.querySelector("header.header") ||
      document.querySelector("header.navbar") ||
      document.querySelector("nav.navbar");

    if (!host) {
      host = document.createElement("div");
      host.style.cssText = "position:fixed;top:14px;right:16px;z-index:2147483000;";
      document.body.appendChild(host);
    }

    var signInLinks = host.querySelectorAll('a[href="login.html"],a[href="/login.html"],a[href="login"],a[href="/login"]');
    signInLinks.forEach(function (link) { link.remove(); });
    host.appendChild(createWidget());
  }

  function createWidget() {
    var root = document.createElement("div");
    root.id = "gw-account-widget";
    root.setAttribute("aria-live", "polite");
    root.setAttribute("aria-busy", "true");
    return root;
  }

  function addProfileIcon(parent) {
    var icon = document.createElement("span");
    icon.className = "gw-account-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.innerHTML = '<svg viewBox="0 0 24 24" focusable="false"><path d="M12 12a4.25 4.25 0 1 0 0-8.5A4.25 4.25 0 0 0 12 12Zm0 2c-4.1 0-7.5 2.1-7.5 4.7V21h15v-2.3c0-2.6-3.4-4.7-7.5-4.7Z"/></svg>';
    parent.appendChild(icon);
  }

  function renderGuest() {
    var root = document.getElementById("gw-account-widget");
    if (!root) return;
    root.replaceChildren();
    root.setAttribute("aria-busy", "false");
    var links = document.createElement("span");
    links.className = "gw-account-guest";
    var login = document.createElement("a");
    login.href = "/login.html";
    login.textContent = "Sign in";
    var register = document.createElement("a");
    register.href = "/register.html";
    register.className = "gw-account-register";
    register.textContent = "Register";
    links.append(login, register);
    root.appendChild(links);
  }

  function renderUser(user) {
    var root = document.getElementById("gw-account-widget");
    if (!root) return;
    root.replaceChildren();
    root.setAttribute("aria-busy", "false");

    var name = String(user.name || user.email || "Account").trim();
    var email = String(user.email || "").trim();
    var button = document.createElement("button");
    button.type = "button";
    button.className = "gw-account-toggle";
    button.setAttribute("aria-haspopup", "menu");
    button.setAttribute("aria-expanded", "false");
    button.setAttribute("aria-controls", "gw-account-menu");
    addProfileIcon(button);

    var label = document.createElement("span");
    label.className = "gw-account-name";
    label.textContent = name;
    label.title = name;
    var arrow = document.createElement("span");
    arrow.className = "gw-account-arrow";
    arrow.textContent = "▼";
    arrow.setAttribute("aria-hidden", "true");
    button.append(label, arrow);

    var menu = document.createElement("div");
    menu.id = "gw-account-menu";
    menu.className = "gw-account-menu";
    menu.setAttribute("role", "menu");
    menu.hidden = true;

    var summary = document.createElement("div");
    summary.className = "gw-account-summary";
    var summaryName = document.createElement("span");
    summaryName.className = "gw-account-summary-name";
    summaryName.textContent = name;
    summary.appendChild(summaryName);
    if (email) {
      var summaryEmail = document.createElement("span");
      summaryEmail.className = "gw-account-email";
      summaryEmail.textContent = email;
      summary.appendChild(summaryEmail);
    }

    var logout = document.createElement("button");
    logout.type = "button";
    logout.className = "gw-account-logout";
    logout.setAttribute("role", "menuitem");
    logout.textContent = "↪  Logout";
    menu.append(summary, logout);
    root.append(button, menu);

    var closeTimer = null;
    function closeMenu(returnFocus) {
      button.setAttribute("aria-expanded", "false");
      menu.classList.add("gw-account-opening");
      window.clearTimeout(closeTimer);
      closeTimer = window.setTimeout(function () {
        menu.hidden = true;
      }, 170);
      if (returnFocus) button.focus();
    }

    button.addEventListener("click", function () {
      var open = button.getAttribute("aria-expanded") === "true";
      window.clearTimeout(closeTimer);
      if (open) {
        closeMenu(false);
        return;
      }
      menu.hidden = false;
      menu.classList.add("gw-account-opening");
      window.requestAnimationFrame(function () {
        menu.classList.remove("gw-account-opening");
      });
      button.setAttribute("aria-expanded", "true");
    });

    document.addEventListener("click", function (event) {
      if (!root.contains(event.target) && button.getAttribute("aria-expanded") === "true") {
        closeMenu(false);
      }
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && button.getAttribute("aria-expanded") === "true") {
        closeMenu(true);
      }
    });

    logout.addEventListener("click", async function () {
      logout.disabled = true;
      logout.textContent = "Logging out…";
      try {
        var response = await fetch("/api/logout", {
          method: "POST",
          credentials: "include",
          cache: "no-store"
        });
        if (!response.ok) throw new Error("Logout failed (" + response.status + ").");
        try {
          localStorage.removeItem("gw_user");
          localStorage.removeItem("gwPaymentVerified");
          localStorage.removeItem("gwReceiptData");
        } catch (storageError) {
          console.warn("Could not clear cached account state:", storageError);
        }
        window.location.assign("/");
      } catch (error) {
        console.error("Could not end the server session:", error);
        var errorMessage = document.createElement("p");
        errorMessage.className = "gw-account-error";
        errorMessage.setAttribute("role", "alert");
        errorMessage.textContent = error.message || "Logout failed. Please try again.";
        menu.querySelector(".gw-account-error")?.remove();
        menu.appendChild(errorMessage);
        logout.disabled = false;
        logout.textContent = "↪  Logout";
      }
    });
  }

  fetch("/api/me", { cache: "no-store", credentials: "include" })
    .then(function (response) {
      if (!response.ok) throw new Error("Account check failed (" + response.status + ").");
      return response.json();
    })
    .then(function (data) {
      if (data.authenticated && data.user) renderUser(data.user);
      else renderGuest();
    })
    .catch(function (error) {
      console.warn("Could not confirm the signed-in account with the server:", error);
      renderGuest();
    });
})();
</script>
"""


def inject_current_account_widget(response):
    """Add the account dropdown to HTML responses without changing other content."""
    if response.mimetype != "text/html":
        return response

    response.direct_passthrough = False
    html = response.get_data(as_text=True)
    if "data-gw-current-account-widget" in html:
        return response

    closing_body = html.lower().rfind("</body>")
    if closing_body >= 0:
        html = html[:closing_body] + _CURRENT_ACCOUNT_WIDGET + html[closing_body:]
    else:
        html += _CURRENT_ACCOUNT_WIDGET
    response.set_data(html)
    return response
