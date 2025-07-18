// static/js/task_details.js

document.addEventListener('DOMContentLoaded', function() {
    // --- GLOBAL VARIABLES (passed from template) ---
    const allAttachments = JSON.parse(document.getElementById('task-data').dataset.allAttachments);
    const technicianList = JSON.parse(document.getElementById('task-data').dataset.technicianList);
    const techReportsHistory = JSON.parse(document.getElementById('task-data').dataset.techReportsHistory);
    const taskId = document.getElementById('task-data').dataset.taskId;
    const csrfToken = document.getElementById('task-data').dataset.csrfToken;

    // --- INITIALIZERS ---
    initializeLightBox();
    initializeTribute();
    initializeModals();
    initializeEventListeners();

    // --- FUNCTIONS ---
    function initializeLightBox() { /* ... Lightbox logic from update_task_details.html ... */ }
    function initializeTribute() { /* ... Tribute logic ... */ }
    function initializeModals() { /* ... Technician and Edit modals logic ... */ }
    
    function initializeEventListeners() {
        // Add event listener for delete report buttons with confirmation
        document.querySelectorAll('.delete-report-btn').forEach(button => {
            button.addEventListener('click', function(event) {
                const reportIndex = event.currentTarget.dataset.reportIndex;
                Swal.fire({
                    title: 'คุณแน่ใจหรือไม่?',
                    text: "คุณต้องการลบรายงานนี้ใช่ไหม? การกระทำนี้ไม่สามารถย้อนกลับได้!",
                    icon: 'warning',
                    showCancelButton: true,
                    confirmButtonColor: '#d33',
                    cancelButtonColor: '#6c757d',
                    confirmButtonText: 'ใช่, ลบเลย!',
                    cancelButtonText: 'ยกเลิก'
                }).then((result) => {
                    if (result.isConfirmed) {
                        deleteReport(reportIndex);
                    }
                });
            });
        });

        // Other event listeners (forms, etc.)
    }

    async function deleteReport(reportIndex) { /* ... fetch logic to call delete API ... */ }
    
    // All other JS functions from update_task_details.html go here...
});