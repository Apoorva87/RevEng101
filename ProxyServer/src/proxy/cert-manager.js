const forge = require('node-forge');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const CERTS_DIR = path.join(__dirname, '..', '..', 'certs');
const HOSTS_DIR = path.join(CERTS_DIR, 'hosts');
const CA_KEY_PATH = path.join(CERTS_DIR, 'ca.key');
const CA_CERT_PATH = path.join(CERTS_DIR, 'ca.crt');

class CertManager {
  constructor() {
    this.caKey = null;
    this.caCert = null;
    this.hostCache = new Map(); // hostname -> { key, cert }
  }

  init() {
    fs.mkdirSync(CERTS_DIR, { recursive: true });
    fs.mkdirSync(HOSTS_DIR, { recursive: true });

    if (fs.existsSync(CA_KEY_PATH) && fs.existsSync(CA_CERT_PATH)) {
      this.caKey = forge.pki.privateKeyFromPem(fs.readFileSync(CA_KEY_PATH, 'utf8'));
      this.caCert = forge.pki.certificateFromPem(fs.readFileSync(CA_CERT_PATH, 'utf8'));
      console.log('[CertManager] Loaded existing CA certificate');
    } else {
      this._generateCA();
      console.log('[CertManager] Generated new CA certificate');
    }

    this._printTrustInstructions();
    return this;
  }

  _generateCA() {
    const keys = forge.pki.rsa.generateKeyPair(2048);
    const cert = forge.pki.createCertificate();

    cert.publicKey = keys.publicKey;
    cert.serialNumber = '01';
    cert.validity.notBefore = new Date();
    cert.validity.notAfter = new Date();
    cert.validity.notAfter.setFullYear(cert.validity.notBefore.getFullYear() + 10);

    const attrs = [
      { name: 'commonName', value: 'ProxyServer CA' },
      { name: 'organizationName', value: 'ProxyServer' },
      { name: 'countryName', value: 'US' },
    ];
    cert.setSubject(attrs);
    cert.setIssuer(attrs);

    cert.setExtensions([
      { name: 'basicConstraints', cA: true },
      { name: 'keyUsage', keyCertSign: true, digitalSignature: true, cRLSign: true },
      { name: 'subjectKeyIdentifier' },
    ]);

    cert.sign(keys.privateKey, forge.md.sha256.create());

    fs.writeFileSync(CA_KEY_PATH, forge.pki.privateKeyToPem(keys.privateKey));
    fs.writeFileSync(CA_CERT_PATH, forge.pki.certificateToPem(cert));

    this.caKey = keys.privateKey;
    this.caCert = cert;
  }

  getHostCert(hostname) {
    // Check memory cache
    if (this.hostCache.has(hostname)) {
      return this.hostCache.get(hostname);
    }

    // Check disk cache
    const safeHost = hostname.replace(/[^a-zA-Z0-9.-]/g, '_');
    const keyPath = path.join(HOSTS_DIR, safeHost + '.key');
    const certPath = path.join(HOSTS_DIR, safeHost + '.crt');

    if (fs.existsSync(keyPath) && fs.existsSync(certPath)) {
      const result = {
        key: fs.readFileSync(keyPath, 'utf8'),
        cert: fs.readFileSync(certPath, 'utf8'),
      };
      this.hostCache.set(hostname, result);
      return result;
    }

    // Generate new cert
    const result = this._generateHostCert(hostname);
    fs.writeFileSync(keyPath, result.key);
    fs.writeFileSync(certPath, result.cert);
    this.hostCache.set(hostname, result);
    return result;
  }

  _generateHostCert(hostname) {
    const keys = forge.pki.rsa.generateKeyPair(2048);
    const cert = forge.pki.createCertificate();

    cert.publicKey = keys.publicKey;
    cert.serialNumber = crypto.randomBytes(16).toString('hex');
    cert.validity.notBefore = new Date();
    cert.validity.notAfter = new Date();
    cert.validity.notAfter.setFullYear(cert.validity.notBefore.getFullYear() + 2);

    const attrs = [
      { name: 'commonName', value: hostname },
      { name: 'organizationName', value: 'ProxyServer' },
    ];
    cert.setSubject(attrs);
    cert.setIssuer(this.caCert.subject.attributes);

    cert.setExtensions([
      { name: 'basicConstraints', cA: false },
      { name: 'keyUsage', digitalSignature: true, keyEncipherment: true },
      { name: 'extKeyUsage', serverAuth: true },
      {
        name: 'subjectAltName',
        altNames: [{ type: 2, value: hostname }], // DNS
      },
      { name: 'subjectKeyIdentifier' },
      { name: 'authorityKeyIdentifier', keyIdentifier: true },
    ]);

    cert.sign(this.caKey, forge.md.sha256.create());

    return {
      key: forge.pki.privateKeyToPem(keys.privateKey),
      cert: forge.pki.certificateToPem(cert),
    };
  }

  _printTrustInstructions() {
    console.log('');
    console.log('=== HTTPS MITM Setup ===');
    console.log(`CA certificate: ${CA_CERT_PATH}`);
    console.log('');
    console.log('To trust the CA certificate:');
    console.log('  macOS:   sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain ' + CA_CERT_PATH);
    console.log('  Firefox: Preferences → Privacy & Security → Certificates → Import');
    console.log('  Chrome:  chrome://settings/certificates → Authorities → Import');
    console.log('');
  }

  getCACertPath() {
    return CA_CERT_PATH;
  }
}

module.exports = CertManager;
