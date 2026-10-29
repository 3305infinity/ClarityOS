const WebSocket = require('ws');
const ws = new WebSocket('ws://localhost:8000/ws');

ws.on('open', function open() {
  console.log('Node client connected!');
  ws.send(JSON.stringify({ type: "test" }));
});

ws.on('message', function incoming(data) {
  console.log('Received:', data.toString());
});

ws.on('error', function error(err) {
  console.error('Connection error:', err.message);
});
