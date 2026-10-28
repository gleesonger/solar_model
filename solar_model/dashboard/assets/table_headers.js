// Keep the real header aligned with horizontal scrolling, bounded by its table.
(() => {
    let pending = false;
    const update = () => {
        pending = false;
        const topbar = document.querySelector('.dashboard-topbar');
        const top = topbar ? Math.max(0, topbar.getBoundingClientRect().bottom) : 0;
        document.querySelectorAll('.q-table__container table').forEach(table => {
            const header = table.querySelector('thead');
            if (!header || !table.getClientRects().length) return;
            const bounds = table.getBoundingClientRect();
            const offset = Math.max(0, Math.min(top - bounds.top, bounds.height - header.offsetHeight));
            header.style.transform = `translateY(${offset}px)`;
        });
    };
    const schedule = () => {
        if (!pending) {
            pending = true;
            requestAnimationFrame(update);
        }
    };
    document.addEventListener('scroll', schedule, {capture: true, passive: true});
    window.addEventListener('resize', schedule, {passive: true});
    document.addEventListener('DOMContentLoaded', () => {
        new MutationObserver(schedule).observe(document.body, {childList: true, subtree: true});
        schedule();
    });
})();
