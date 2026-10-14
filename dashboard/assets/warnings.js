(() => {
    let lastHeartbeat = 0;
    let lastCheck = Date.now();
    let observedSocket;

    function isStale(metadata, timestampKey, limitKey) {
        const timestamp = Date.parse(metadata?.dataset[timestampKey] || '');
        const limit = Number(metadata?.dataset[limitKey]);
        return !Number.isFinite(timestamp) || !Number.isFinite(limit)
            || (Date.now() - timestamp) / 1000 > limit;
    }

    function invalidateConnection() {
        lastHeartbeat = 0;
        updateDashboardFreshness();
    }

    function updateDashboardFreshness() {
        const now = Date.now();
        const metadata = document.getElementById('dashboard-warning-data');
        const heartbeatTimeout = Number(metadata?.dataset.heartbeatTimeoutMs) || Infinity;
        // Timers are paused on phones. Demand a new heartbeat after a long gap.
        if (now - lastCheck > heartbeatTimeout) lastHeartbeat = 0;
        lastCheck = now;

        const socket = window.socket;
        if (socket && socket !== observedSocket) {
            observedSocket = socket;
            socket.on('disconnect', invalidateConnection);
            socket.on('connect', invalidateConnection);
        }

        let message = '';
        if (!navigator.onLine) {
            message = 'Offline — displayed data is stale. Updates will resume when connected.';
        } else if (!socket?.connected) {
            message = 'Connection lost — displayed data may be stale. Reconnecting…';
        } else if (!lastHeartbeat || now - lastHeartbeat > heartbeatTimeout) {
            message = 'Waiting for server updates — displayed data may be stale.';
        } else if (isStale(metadata, 'lastFullUpdate', 'fullStaleAfterSeconds')) {
            message = 'Collected data is out of date — waiting for new data.';
        } else if (isStale(metadata, 'lastLiveUpdate', 'liveStaleAfterSeconds')) {
            message = 'Live readings are unavailable or stale — waiting for new readings.';
        }

        const banner = document.getElementById('dashboard-warning-banner');
        const text = document.getElementById('dashboard-warning-message');
        if (banner && text) {
            text.textContent = message;
            banner.hidden = !message;
        }
    }

    window.dashboardHeartbeat = () => {
        // Check for a suspended tab before accepting this fresh server message.
        updateDashboardFreshness();
        lastHeartbeat = Date.now();
        updateDashboardFreshness();
    };

    function resume() {
        invalidateConnection();
    }

    window.addEventListener('offline', invalidateConnection);
    window.addEventListener('online', resume);
    window.addEventListener('pageshow', resume);
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') resume();
    });
    document.addEventListener('DOMContentLoaded', updateDashboardFreshness);
    setInterval(updateDashboardFreshness, 1000);

})();
