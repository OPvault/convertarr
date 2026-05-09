// Tiny toast queue. Renders a stack of dismissable cards in the
// bottom-right corner. No deps — slide-in is CSS, lifecycle is timers.
//
// Usage:
//   convertarrToast("Saved")
//   convertarrToast("No workflows yet", { type: "warning",
//                                          action: { label: "Set up", href: "/x" } })
//
// Types: "info" (default) | "success" | "warning" | "error"

(function () {
    let container = null;

    function ensureContainer() {
        if (container) return container;
        container = document.createElement("div");
        container.id = "convertarr-toasts";
        document.body.appendChild(container);
        return container;
    }

    function dismiss(toast) {
        if (!toast.parentNode) return;
        toast.classList.add("toast-leaving");
        // Match the CSS transition duration before removing from the DOM —
        // shorter and the slide-out animation gets cut off.
        setTimeout(() => toast.remove(), 250);
    }

    window.convertarrToast = function (message, opts = {}) {
        const type = opts.type || "info";
        const duration = opts.duration ?? 5000;
        const action = opts.action || null;

        const toast = document.createElement("div");
        toast.className = "toast toast-" + type;

        const body = document.createElement("div");
        body.className = "toast-body";
        body.textContent = message;
        toast.appendChild(body);

        if (action && action.label && action.href) {
            const link = document.createElement("a");
            link.className = "toast-action";
            link.href = action.href;
            link.textContent = action.label;
            toast.appendChild(link);
        }

        const close = document.createElement("button");
        close.className = "toast-close";
        close.type = "button";
        close.setAttribute("aria-label", "Dismiss");
        close.textContent = "×";
        close.addEventListener("click", (e) => {
            e.stopPropagation();
            dismiss(toast);
        });
        toast.appendChild(close);

        ensureContainer().appendChild(toast);
        // Trigger the slide-in by flipping a class on the next frame —
        // browsers won't animate a freshly-inserted node otherwise.
        requestAnimationFrame(() => toast.classList.add("toast-in"));

        if (duration > 0) {
            setTimeout(() => dismiss(toast), duration);
        }
        return toast;
    };
})();
