// static/js/form.js

document.addEventListener('DOMContentLoaded', function() {
    let customerDatabase = [];

    // --- INITIALIZATION ---
    async function initialize() {
        await fetchCustomerDatabase();
        initializeAutocomplete(document.getElementById("customer"), customerDatabase);
        initializeDateTimePicker();
        initializeTribute();
        initializeGeolocation();
        initializeFormValidation();
    }

    // --- DATA FETCHING ---
    async function fetchCustomerDatabase() {
        try {
            const response = await fetch("/api/customers");
            if (response.ok) {
                customerDatabase = await response.json();
            } else {
                console.error("Failed to load customer database.");
            }
        } catch (error) {
            console.error("Error fetching customer database:", error);
        }
    }
    
    // --- COMPONENT INITIALIZERS ---
    function initializeAutocomplete(inputElement, data) {
        // ... (Autocomplete logic from form.html)
    }

    function initializeDateTimePicker() {
        flatpickr(".datetimepicker", {
            enableTime: true,
            dateFormat: "Y-m-d H:i",
            locale: "th",
        });
    }

    function initializeTribute() {
        // This requires `task_detail_snippets` to be passed from the template
        // We'll add it as a data attribute on the script tag in the HTML
        const snippetsData = document.querySelector('script[data-snippets]').dataset.snippets;
        if (snippetsData) {
            const taskDetailSnippets = JSON.parse(snippetsData);
            const tributeTask = new Tribute({ /* ... Tribute options ... */ });
            tributeTask.attach(document.getElementById('task_title'));
        }
    }

    function initializeGeolocation() {
        // ... (Geolocation logic from form.html)
    }
    
    function initializeFormValidation() {
        // ... (Bootstrap validation logic)
    }

    // Attach the main submission handler to the form
    const createTaskForm = document.getElementById('createTaskForm');
    if (createTaskForm) {
        createTaskForm.addEventListener('submit', handleFormSubmission);
    }
    
    initialize();
});

// --- MAIN FORM SUBMISSION LOGIC ---
async function handleFormSubmission(event) {
    event.preventDefault();
    const form = event.target;
    // ... (The entire handleFormSubmission logic from form.html)
}