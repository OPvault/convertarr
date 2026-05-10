// Lazy-load poster images on the Sonarr/Radarr grid pages.
// Posters carry their URL in data-poster; we only set background-image when
// the tile is about to enter the viewport. Big libraries (1000+ posters)
// otherwise fire a thousand HTTP requests on page load even though the user
// only sees a couple of rows.
(function () {
    const ROOT_MARGIN = "400px";

    function load(el) {
        const url = el.getAttribute("data-poster");
        if (!url) return;
        el.style.backgroundImage = "url('" + url.replace(/'/g, "\\'") + "')";
        el.removeAttribute("data-poster");
    }

    function init() {
        const targets = document.querySelectorAll(".poster-image[data-poster]");
        if (!targets.length) return;

        if (!("IntersectionObserver" in window)) {
            targets.forEach(load);
            return;
        }

        const io = new IntersectionObserver(function (entries) {
            entries.forEach(function (entry) {
                if (!entry.isIntersecting) return;
                load(entry.target);
                io.unobserve(entry.target);
            });
        }, { rootMargin: ROOT_MARGIN });

        targets.forEach(function (el) { io.observe(el); });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
