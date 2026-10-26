import { existsSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const bundledPython = path.join(os.homedir(), '.cache', 'codex-runtimes',
  'codex-primary-runtime', 'dependencies', 'python', 'python.exe');

export const PYTHON = process.env.SOP_PYTHON_PATH ||
  (existsSync(bundledPython) ? bundledPython : (process.platform === 'win32' ? 'python' : 'python3'));
