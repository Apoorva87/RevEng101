const { spawn } = require('child_process');
const crypto = require('crypto');
const EventEmitter = require('events');

class ClaudeSession extends EventEmitter {
  constructor() {
    super();
    this.available = false;
    this._activeProcess = null;
    this.sessionId = crypto.randomUUID();
    this.sessionName = `ProxyChat-${this.sessionId.slice(0, 4)}`;
    this._messageCount = 0;
    this._systemPromptSet = false;
    this._checkAvailability();
  }

  _checkAvailability() {
    try {
      const result = spawn('which', ['claude'], { stdio: ['pipe', 'pipe', 'pipe'] });
      result.on('close', (code) => {
        this.available = code === 0;
        if (!this.available) {
          console.log('[Chat] Claude CLI not found in PATH — chat disabled');
        } else {
          console.log('[Chat] Claude CLI found — chat enabled');
        }
      });
      result.on('error', () => {
        this.available = false;
      });
    } catch (e) {
      this.available = false;
    }
  }

  isAvailable() {
    return this.available;
  }

  /**
   * Send a user message to Claude using persistent session management.
   * First message uses --session-id + --system-prompt; subsequent messages use --resume.
   * @param {string} userMessage - The user's text
   * @param {object} opts - { systemPrompt, dynamicContext }
   * @returns {Promise<string>} The full assistant response
   */
  async send(userMessage, { systemPrompt, dynamicContext } = {}) {
    if (!this.available) {
      throw new Error('Claude CLI not available');
    }

    // Build the message text: prepend dynamic context if present
    const messageText = dynamicContext
      ? `${dynamicContext}\n\n${userMessage}`
      : userMessage;

    return new Promise((resolve, reject) => {
      let fullResponse = '';
      let errorOutput = '';

      const args = ['-p', messageText];

      if (this._messageCount === 0) {
        // First message: establish the session
        args.push('--session-id', this.sessionId);
        args.push('-n', this.sessionName);
        if (systemPrompt) {
          args.push('--system-prompt', systemPrompt);
        }
      } else {
        // Subsequent messages: resume existing session
        args.push('--resume', this.sessionId);
      }

      args.push('--output-format', 'stream-json');
      args.push('--max-turns', '1');
      args.push('--verbose');

      const proc = spawn('claude', args, {
        stdio: ['pipe', 'pipe', 'pipe'],
        cwd: process.cwd(),
        env: { ...process.env },
      });

      this._activeProcess = proc;

      let buffer = '';

      proc.stdout.on('data', (chunk) => {
        buffer += chunk.toString();
        const lines = buffer.split('\n');
        buffer = lines.pop();

        for (const line of lines) {
          if (!line.trim()) continue;
          try {
            const event = JSON.parse(line);
            this._handleStreamEvent(event, (text) => {
              fullResponse += text;
            });
          } catch (e) {
            if (line.trim()) {
              fullResponse += line;
              this.emit('chunk', line);
            }
          }
        }
      });

      proc.stderr.on('data', (chunk) => {
        errorOutput += chunk.toString();
      });

      proc.on('close', (code) => {
        this._activeProcess = null;
        // Process remaining buffer
        if (buffer.trim()) {
          try {
            const event = JSON.parse(buffer);
            this._handleStreamEvent(event, (text) => {
              fullResponse += text;
            });
          } catch (e) {
            if (buffer.trim()) {
              fullResponse += buffer.trim();
            }
          }
        }

        if (code !== 0 && !fullResponse) {
          const err = new Error(`Claude process exited with code ${code}: ${errorOutput.slice(0, 500)}`);
          this.emit('error', err);
          // Don't increment _messageCount on error — message wasn't recorded in session
          reject(err);
          return;
        }

        // Success — increment message count
        this._messageCount++;

        this.emit('done', fullResponse);
        resolve(fullResponse);
      });

      proc.on('error', (err) => {
        this._activeProcess = null;
        this.emit('error', err);
        reject(err);
      });

      proc.stdin.end();
    });
  }

  _handleStreamEvent(event, appendText) {
    if (!event || !event.type) return;

    switch (event.type) {
      case 'assistant':
        if (event.subtype === 'text' && event.text) {
          appendText(event.text);
          this.emit('chunk', event.text);
        }
        break;
      case 'content_block_delta':
        if (event.delta && event.delta.text) {
          appendText(event.delta.text);
          this.emit('chunk', event.delta.text);
        }
        break;
      case 'tool_use':
      case 'tool':
        this.emit('action', {
          action: event.type,
          detail: event.name || event.tool || 'unknown tool',
        });
        break;
      case 'result':
        if (event.result) {
          appendText(event.result);
          this.emit('chunk', event.result);
        }
        break;
    }
  }

  reset() {
    this.kill();
    this.sessionId = crypto.randomUUID();
    this.sessionName = `ProxyChat-${this.sessionId.slice(0, 4)}`;
    this._messageCount = 0;
    this._systemPromptSet = false;
    this.emit('reset');
  }

  async compact() {
    // Claude CLI manages its own context; compaction is a no-op now.
    // Could send a summarization message if needed in the future.
    return 'Session history is managed by Claude CLI. No manual compaction needed.';
  }

  kill() {
    if (this._activeProcess) {
      this._activeProcess.kill('SIGTERM');
      this._activeProcess = null;
    }
  }

  get isFirstMessage() {
    return this._messageCount === 0;
  }

  get sessionActive() {
    return this._messageCount > 0;
  }

  get tokenEstimate() {
    // CLI manages history internally; return nominal value
    return 0;
  }
}

module.exports = ClaudeSession;
