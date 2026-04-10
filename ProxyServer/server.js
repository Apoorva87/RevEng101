const TrafficStore = require('./src/traffic/traffic-store');
const ProxyServer = require('./src/proxy/proxy-server');
const DashboardServer = require('./src/dashboard/dashboard-server');
const WSBridge = require('./src/dashboard/ws-bridge');
const CertManager = require('./src/proxy/cert-manager');
const TLSHandler = require('./src/proxy/tls-handler');
const RequestInterceptor = require('./src/proxy/request-interceptor');
const ResponseInterceptor = require('./src/proxy/response-interceptor');
const RuleEngine = require('./src/rules/rule-engine');
const RuleStore = require('./src/rules/rule-store');
const ChatHandler = require('./src/chat/chat-handler');

const PROXY_PORT = parseInt(process.env.PROXY_PORT, 10) || 9080;
const DASHBOARD_PORT = parseInt(process.env.DASHBOARD_PORT, 10) || 9081;

async function main() {
  const trafficStore = new TrafficStore(5000);

  // Initialize certificate manager for HTTPS MITM
  const certManager = new CertManager();
  certManager.init();

  // Initialize rule engine and interceptors
  const ruleStore = new RuleStore();
  const ruleEngine = new RuleEngine(ruleStore);
  const requestInterceptor = new RequestInterceptor({ ruleEngine, trafficStore });
  const responseInterceptor = new ResponseInterceptor({ ruleEngine, trafficStore });

  // Initialize TLS handler
  const tlsHandler = new TLSHandler({ certManager, trafficStore, requestInterceptor, responseInterceptor });

  // Start proxy
  const proxy = new ProxyServer({
    port: PROXY_PORT,
    trafficStore,
    requestInterceptor,
    responseInterceptor,
  });
  proxy.tlsHandler = tlsHandler;
  await proxy.start();

  // Start dashboard
  const dashboard = new DashboardServer({ port: DASHBOARD_PORT, trafficStore });
  dashboard.ruleStore = ruleStore;
  dashboard.requestInterceptor = requestInterceptor;
  dashboard.responseInterceptor = responseInterceptor;
  await dashboard.start();

  // Initialize chat handler
  const chatHandler = new ChatHandler({
    trafficStore,
    ruleStore,
    projectRoot: __dirname,
  });
  dashboard.chatHandler = chatHandler;

  // Attach WebSocket bridge to dashboard HTTP server
  const wsBridge = new WSBridge({ server: dashboard.server, trafficStore, chatHandler });

  console.log('');
  console.log('=== ProxyServer Ready ===');
  console.log(`  Proxy:     http://localhost:${PROXY_PORT}`);
  console.log(`  Dashboard: http://localhost:${DASHBOARD_PORT}`);
  console.log('');
  console.log('Configure your browser/system HTTP proxy to localhost:' + PROXY_PORT);
  console.log('Then open the dashboard URL in your browser.');
  console.log('');

  // Graceful shutdown
  const shutdown = () => {
    console.log('\nShutting down...');
    chatHandler.kill();
    Promise.all([proxy.stop(), dashboard.stop()]).then(() => process.exit(0));
  };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}

main().catch(err => {
  console.error('Fatal error:', err);
  process.exit(1);
});
