    window.flashLiveUpdateIndicator = () => {
        const indicator = document.querySelector('.live-update-indicator');
        const message = document.querySelector('.live-update-message');
        if (!indicator) return;
        indicator.style.display = 'inline-flex';
        if (message) message.style.display = 'none';
        window.clearTimeout(window.liveUpdateIdleTimer);
        window.liveUpdateIdleTimer = window.setTimeout(() => {
            indicator.style.display = 'none';
            if (message) message.style.display = '';
            window.liveUpdateIdleTimer = null;
        }, 3000);
    };
