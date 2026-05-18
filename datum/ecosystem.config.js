// ============================================================
// DATUM — PM2 Ecosystem Config
// FORGE · ANVIL · PANEL
//
// Start all systems: pm2 start ecosystem.config.js
// Stop all:         pm2 stop all
// View logs:        pm2 logs datum-forge
// ============================================================

module.exports = {
  apps: [

    // ── FORGE — Document Intelligence ──────────────────────
    {
      name:         "datum-forge",
      script:       "./forge/forge.py",
      interpreter:  "python3",
      args:         "--config ./forge/config/forge.yaml --path ./forge",
      cwd:          __dirname,
      watch:        false,
      autorestart:  true,
      max_restarts: 10,
      restart_delay:5000,
      env: { PYTHONUNBUFFERED: "1", FORGE_LOG_LEVEL: "INFO" },
      error_file:   "./logs/forge-error.log",
      out_file:     "./logs/forge-out.log",
      merge_logs:   true,
      log_date_format: "YYYY-MM-DD HH:mm:ss",
    },

    // ── ANVIL — Code Verification ───────────────────────────
    {
      name:         "datum-anvil",
      script:       "./anvil/anvil.py",
      interpreter:  "python3",
      args:         "--config ./anvil/config/anvil.yaml",
      cwd:          __dirname,
      watch:        false,
      autorestart:  true,
      max_restarts: 10,
      restart_delay:5000,
      env: { PYTHONUNBUFFERED: "1" },
      error_file:   "./logs/anvil-error.log",
      out_file:     "./logs/anvil-out.log",
      merge_logs:   true,
      log_date_format: "YYYY-MM-DD HH:mm:ss",
    },

    // ── PANEL — Command Center ──────────────────────────────
    {
      name:         "datum-panel",
      script:       "./panel/server.py",
      interpreter:  "python3",
      args:         "--port 4000 --forge ./forge --anvil ./anvil",
      cwd:          __dirname,
      watch:        false,
      autorestart:  true,
      max_restarts: 25,
      restart_delay:2000,
      env: { PYTHONUNBUFFERED: "1" },
      error_file:   "./logs/panel-error.log",
      out_file:     "./logs/panel-out.log",
      merge_logs:   true,
      log_date_format: "YYYY-MM-DD HH:mm:ss",
    },

    // ── FORGE WATCHER — Real-time file intake ───────────────
    {
      name:         "datum-watcher",
      script:       "./forge/watcher.py",
      interpreter:  "python3",
      args:         "--path ./forge/staging --config ./forge/config/forge.yaml",
      cwd:          __dirname,
      watch:        false,
      autorestart:  true,
      env: { PYTHONUNBUFFERED: "1" },
      error_file:   "./logs/watcher-error.log",
      out_file:     "./logs/watcher-out.log",
    },

  ]
}
