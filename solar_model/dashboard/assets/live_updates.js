(() => {
    let expiresAt = 0;
    let healthy = false;

    function renderReadingTimestamp() {
        const label = document.querySelector('.live-update-message');
        if (!label) return;
        const timestamp = document.getElementById('dashboard-warning-data')?.dataset.lastLiveUpdate;
        const reading = new Date(timestamp || '');
        let text = 'Last reading: awaiting live data';
        if (Number.isFinite(reading.getTime())) {
            const pad = value => String(value).padStart(2, '0');
            // Date getters use this browser's timezone, including its DST rules.
            text = `Last reading: ${reading.getFullYear()}-${pad(reading.getMonth() + 1)}-${pad(reading.getDate())}`
                + ` ${pad(reading.getHours())}:${pad(reading.getMinutes())}:${pad(reading.getSeconds())}`;
        }
        if (label.textContent !== text) label.textContent = text;
    }

    function clearIndicator() {
        expiresAt = 0;
        document.querySelector('.live-update-indicator')?.classList.remove('is-updating');
    }

    function checkIndicator() {
        renderReadingTimestamp();
        if (!healthy || !navigator.onLine || !window.socket?.connected
            || document.visibilityState !== 'visible' || Date.now() >= expiresAt) {
            clearIndicator();
        }
    }

    window.setLiveUpdateHealth = value => {
        healthy = value;
        checkIndicator();
    };

    window.flashLiveUpdateIndicator = () => {
        const indicator = document.querySelector('.live-update-indicator');
        if (!indicator) return;
        expiresAt = Date.now() + 3000;
        checkIndicator();
        if (!expiresAt) return;
        indicator.classList.remove('is-updating');
        // Restart a finite CSS animation. Its final state hides the indicator
        // without relying on a JavaScript timer or an animationend callback.
        void indicator.offsetWidth;
        indicator.classList.add('is-updating');
    };

    window.addEventListener('offline', clearIndicator);
    window.addEventListener('pagehide', clearIndicator);
    window.addEventListener('pageshow', checkIndicator);
    document.addEventListener('visibilitychange', checkIndicator);
    document.addEventListener('freeze', clearIndicator);
    document.addEventListener('resume', checkIndicator);
})();
