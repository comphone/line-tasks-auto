// static/js/main.js

/**
 * Displays a dismissible flash message at the top of the page.
 * @param {string} message The message content.
 * @param {string} category The Bootstrap alert category (e.g., 'success', 'danger', 'warning').
 */
function flashMessage(message, category = 'info') {
    const container = document.getElementById('js-flash-message-container');
    if (!container) {
        console.error('Flash message container #js-flash-message-container not found.');
        return;
    }
    const alert = document.createElement('div');
    alert.className = `alert alert-${category} alert-dismissible fade show m-3`;
    alert.role = 'alert';
    alert.innerHTML = `${message}<button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>`;
    
    container.prepend(alert);

    // Auto-dismiss after 5 seconds
    setTimeout(() => {
        const alertInstance = bootstrap.Alert.getInstance(alert);
        if (alertInstance) {
            alertInstance.close();
        }
    }, 5000);
}