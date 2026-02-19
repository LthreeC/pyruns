function enableXtermCopy(elementId) {
    if (!elementId) return;
    
    setTimeout(() => {
        // Safe element retrieval (NiceGUI uses "c" + id prefix for DOM elements)
        const getEl = window.getElement || ((id) => document.getElementById("c" + id));
        const widget = getEl(elementId);
        
        if (widget && widget.terminal) {
            widget.terminal.attachCustomKeyEventHandler((e) => {
                // If Ctrl+C is pressed AND text is selected -> let browser handle it (return false)
                if (e.ctrlKey && (e.key === 'c' || e.key === 'C') && widget.terminal.hasSelection()) {
                    document.execCommand('copy'); // Fallback/Force copy
                    return false; 
                }
                return true;
            });
            console.log("Enabled Ctrl+C copy for xterm", elementId);
        }
    }, 200);
}
