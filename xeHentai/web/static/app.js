/**
 * xeHentai WebUI - Client-side JS
 * WebSocket client, toast notifications, image lightbox
 */

let ws = null;
let wsReconnectTimer = null;

function initWebSocket(token) {
    if (ws && ws.readyState === WebSocket.OPEN) return;

    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    let wsUrl = protocol + "//" + location.host + "/ws";
    if (token) wsUrl += "?token=" + encodeURIComponent(token);

    ws = new WebSocket(wsUrl);

    ws.onopen = function () {
        document.getElementById("ws-status").className =
            "inline-flex items-center px-2 py-1 text-xs font-medium rounded-full bg-green-100 text-green-700";
        document.getElementById("ws-status").textContent = "Connected";
        if (wsReconnectTimer) { clearTimeout(wsReconnectTimer); wsReconnectTimer = null; }
    };

    ws.onclose = function () {
        document.getElementById("ws-status").className =
            "inline-flex items-center px-2 py-1 text-xs font-medium rounded-full bg-red-100 text-red-700";
        document.getElementById("ws-status").textContent = "Disconnected";
        // Auto-reconnect after 3s
        wsReconnectTimer = setTimeout(function () { initWebSocket(token); }, 3000);
    };

    ws.onerror = function () {
        ws.close();
    };

    ws.onmessage = function (event) {
        try {
            let data = JSON.parse(event.data);
            // Dispatch as custom event so page-specific handlers can listen
            window.dispatchEvent(new CustomEvent("ws:" + data.type, { detail: data }));
        } catch (e) {}
    };
}

// Toast notifications
function showToast(message, type) {
    const container = document.getElementById("toast-container");
    if (!container) return;
    const toast = document.createElement("div");
    const colors = {
        success: "bg-green-500",
        error: "bg-red-500",
        info: "bg-blue-500",
        warning: "bg-yellow-500",
    };
    toast.className = (colors[type] || colors.info) + " text-white px-4 py-2 rounded-lg shadow-lg text-sm animate-slide-in";
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(function () {
        toast.style.opacity = "0";
        toast.style.transition = "opacity 0.3s";
        setTimeout(function () { toast.remove(); }, 300);
    }, 3000);
}
