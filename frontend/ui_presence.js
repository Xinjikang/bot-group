/* Page presence is independent of login, project selection and business SSE. */
(() => {
  let connection = null;
  function connect() {
    if (connection) return;
    connection = new EventSource('/desktop/events');
    connection.addEventListener('activate', () => {
      window.focus();
    });
  }
  window.addEventListener('pagehide', () => {
    connection?.close();
    connection = null;
  });
  window.addEventListener('pageshow', connect);
  connect();
})();
