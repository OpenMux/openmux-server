(function(){
      const status = document.getElementById('status');
      const BTN = (id) => document.getElementById(id);
      let csrf = null;
      let current = {};
      const INITIAL_WRITABLE_SECTIONS = window.OMX_CONFIG_EDITOR_BOOTSTRAP.writableSections;
      const INITIAL_WRITABLE_ENFORCED = window.OMX_CONFIG_EDITOR_BOOTSTRAP.writableEnforced;
      let writableSections = new Set(Array.isArray(INITIAL_WRITABLE_SECTIONS) ? INITIAL_WRITABLE_SECTIONS : []);
      let writableEnforced = !!INITIAL_WRITABLE_ENFORCED;
      // Ensure a single open editor globally
      let activeEditorEl = null;
      let activeEditorCleanup = null;
      let activeEditorValidate = null; // returns true if OK, false if missing required; also shows inline error
      let activeEditorSave = null; // programmatically save current editor if valid

      let isDirty = false;
      let isPopulating = false;
      function markDirty(){ if(!isPopulating){ isDirty=true; updateStatus(); } }
      function markClean(){ isDirty=false; updateStatus(); }
      function updateStatus(){
        const btn = document.getElementById('saveBtn');
        if(btn){
           if(isDirty) btn.textContent = 'Save *';
           else btn.textContent = 'Save';
        }
      }
      window.addEventListener('beforeunload', (e)=>{
        if(isDirty){
          e.preventDefault();
          e.returnValue = '';
        }
      });

      function sectionIsWritable(section){
        if(!writableEnforced){
          return true;
        }
        if(!section){
          return writableSections.size > 0;
        }
        if(writableSections.size === 0){
          return false;
        }
        return writableSections.has(section);
      }

      function applyWritableState(){
        const sections = document.querySelectorAll('[data-config-section]');
        sections.forEach((sec)=>{
          const name = sec.getAttribute('data-config-section');
          const writable = sectionIsWritable(name);
          const enforced = writableEnforced;
          sec.classList.toggle('read-only', enforced && !writable);
          let note = sec.querySelector('.read-only-note');
          if(enforced && !writable){
            if(!note){
              note = document.createElement('div');
              note.className = 'read-only-note';
              note.textContent = 'Read-only (security policy)';
              const header = sec.querySelector('.section-header');
              if(header && header.nextSibling){
                sec.insertBefore(note, header.nextSibling);
              } else {
                sec.insertBefore(note, sec.firstChild);
              }
            }
          } else if(note){
            note.remove();
          }
          const controls = sec.querySelectorAll('input, select, textarea, button');
          controls.forEach((ctrl)=>{
            if(ctrl.dataset.allowReadonly === 'true') return;
            if(enforced && !writable){
              if(!ctrl.hasAttribute('data-prev-disabled')){
                ctrl.setAttribute('data-prev-disabled', ctrl.disabled ? '1' : '0');
              }
              ctrl.disabled = true;
            } else if(ctrl.hasAttribute('data-prev-disabled')){
              const prev = ctrl.getAttribute('data-prev-disabled');
              ctrl.disabled = (prev === '1');
              ctrl.removeAttribute('data-prev-disabled');
            }
          });
          sec.querySelectorAll('.tbl-wrap').forEach((wrap)=>{
            wrap.classList.toggle('locked', enforced && !writable);
          });
        });
        const saveBtn = document.getElementById('saveBtn');
        if(saveBtn){
          if(writableEnforced && writableSections.size === 0){
            if(!saveBtn.hasAttribute('data-prev-disabled')){
              saveBtn.setAttribute('data-prev-disabled', saveBtn.disabled ? '1' : '0');
            }
            saveBtn.disabled = true;
          } else if(saveBtn.hasAttribute('data-prev-disabled')){
            const prev = saveBtn.getAttribute('data-prev-disabled');
            saveBtn.disabled = (prev === '1');
            saveBtn.removeAttribute('data-prev-disabled');
          } else {
            saveBtn.disabled = false;
          }
        }
      }

      // issue #58: the access_default row is read-only in this editor; the
      // value comes from /data (security.yaml), never from the save payload
      function setAccessDefaultReadonly(value){
        var v = (value === 'deny') ? 'deny' : 'allow';
        try{ setVal('security.access_default', v); }catch(_e){}
        var badge = document.querySelector('[data-ro-badge]');
        if(badge){ badge.textContent = 'READ-ONLY: ' + v; badge.style.display = 'inline-block'; }
        var ad = document.getElementById('securitiesAdvisory');
        if(ad){ ad.textContent = v === 'deny' ? 'access_default is deny: ports with no group lists admit only admin (denied as denied_by_access_default); list-bearing ports are unaffected.' : ''; ad.style.display = v === 'deny' ? 'block' : 'none'; }
      }

      function setWritableMetadata(sections, enforced){
        if(Array.isArray(sections)){
          writableSections = new Set(sections.map((s)=>String(s)));
        } else {
          writableSections = new Set();
        }
        if(typeof enforced === 'boolean'){
          writableEnforced = enforced;
        }
        applyWritableState();
      }

      function setStatus(ok, msg){ status.innerHTML=''; const el=document.createElement('div'); el.className=ok?'ok':'err'; el.textContent=msg; status.appendChild(el); }

      // Help text for simple fields (non-table inputs)
      const FIELD_HELP = {
        'server.id': 'Unique server identifier used in federation and status; if empty, hostname may be used.',
        'security.access_default': 'Server-wide default for console ports with no group lists (security.yaml). allow = every authenticated user connects; deny = only admin connects. Read-only here; edit config/security.yaml by hand.',
        'server.description': 'Human-readable description shown in UIs and status.',
        'server.control_socket': 'Unix domain socket path for openmuxctl and local control. Default logs/openmux.sock; env OPENMUX_CTL_SOCK overrides.',
        'server.pidfile': 'PID file written on startup to enable kill -HUP/-USR1 control. Default logs/openmux.pid; env OPENMUX_PIDFILE overrides.',

        'logging.level': 'Global log level for the server (DEBUG..CRITICAL).',
        'logging.console': 'Send logs to console/stderr.',
        'logging.file': 'Log file path; leave empty to disable file logging.',
        'logging.log_dir': 'Optional directory to place rotated logs.',
        'logging.max_log_size': 'Max size (bytes) before rotating the log file.',
        'logging.log_backup_count': 'How many rotated files to keep.',

        'client_listener.host': 'Bind address for the TCP console server.',
        'client_listener.enabled': 'Enable or disable the TCP console listener. When disabled, the entire client_listener block is removed from the config.',
        'client_listener.port': 'TCP port for console clients to connect to.',
        'client_listener.max_connections': 'Maximum simultaneous client sessions allowed.',
        'client_listener.connection_timeout': 'Idle connection timeout (seconds).',

        'muxcon.heartbeat_interval': 'Seconds between federation keepalive heartbeats.',
        'muxcon.mpath_primary_stale_sec': 'Mark the current primary path stale after this idle time (seconds).',
        'muxcon.mpath_failover_check_sec': 'Interval to check if failover is needed (seconds).',
        'muxcon.mpath_strategy': 'Multipath strategy to select the active path.',
        'muxcon.mpath_preemptive_promote': 'Preemptively switch back to a preferred path when it recovers.',
        'muxcon.mpath_neighbor_idle_drop_sec': 'Drop idle neighbor sessions after this many seconds.',
        'muxcon.federated_cache_enabled': 'Cache remote port listings to speed up discovery.',
        'muxcon.federated_cache_ttl_sec': 'How long to keep federated cache entries (seconds).',
        'muxcon.federated_cache_path': 'Path to the cache file/directory for federated data.',
        'muxcon.auth_required': 'Require cryptographic authentication for muxcon peers.',
        'muxcon.auth_key_id': 'Key identifier to advertise for muxcon authentication.',
        'muxcon.auth_private_key': 'Inline private key (not recommended); prefer a file path.',

        'web_status.host': 'Bind address for the read-only status server.',
        'web_status.port': 'TCP port for the status server.',
        'web_status.enable_http_api': 'Enable JSON status API endpoints.',
        'web_status.cors_enable': 'Allow cross-origin requests (adds permissive CORS headers).',
        'web_status.enable_fault_injection': 'Enable fault injection endpoints for testing.',

        'web_console.host': 'Bind address for the admin web console.',
        'web_console.port': 'TCP port for the admin web console.',
        'web_console.enable_ui': 'Serve the HTML UI (disable to expose only APIs).',
        'web_console.realm': 'HTTP Basic-Auth realm displayed in login dialogs.',
        'web_console.motd': 'Public message of the day shown on the login page. Multiline is supported; empty hides it.',
        'web_console.logged_in_motd': 'Message of the day for authenticated users (top of the status page). Never shown on the login page; may hold sensitive text.',
        'web_console.static_dir': 'Directory for static assets (xterm, css, js).',
        'web_console.template_dir': 'Directory containing Jinja2 templates.',
        'web_console.session_ttl_seconds': 'How long browser sessions remain valid before re-login is required (seconds).',
        'web_console.enable_probes': 'Expose /healthz /livez /readyz endpoints.',
        'web_console.probes_include_details': 'Return extra JSON (version, uptime, clients) in probes.',
        'web_console.use_tls': 'Serve the web console over HTTPS.',
        'web_console.ssl_cert': 'Path to TLS certificate (PEM).',
        'web_console.ssl_key': 'Path to TLS private key (PEM).',
        'web_console.tls_autogen': 'Autogenerate a self-signed certificate if none is provided.',
        'web_console.tls_dir': 'Directory to store autogen TLS artifacts.',
        'web_console.base_path': 'Base URL path prefix for the web console (e.g., /openmux). Use "/" for root.',
        'web_console.respect_forwarded_prefix': 'Honor X-Forwarded-Prefix header from reverse proxies to derive base path per request.',
        // External auth help
        'auth.extauth.enabled': 'Enable external authentication via the auth helper binary (UNIX accounts and groups).',
        'auth.extauth.service': 'Service name passed to the auth helper (default: openmux).',
        'auth.extauth.helper': 'Path to the auth helper binary. Multi-element helper lists must be edited in YAML.',
        'auth.extauth.timeout': 'Seconds before a helper response is treated as failure (default: 10).',
        'auth.extauth.allow_root': 'Allow root to authenticate via external auth (default: off).',
        'auth.extauth.allowed_users': 'Optional allowlist: only listed users are accepted for external auth.',
        'auth.extauth.groups.admin_group': 'System group name granting admin role.',
        'auth.extauth.groups.write_group': 'System group name granting read-write role.',
        'auth.extauth.groups.read_group': 'System group name granting read-only role.',
        'auth.extauth.default_permission': 'Fallback role for authenticated users with no group mapping.',
      };

      // Merge defaults parsed from docs/DEFAULTS.md
      const DEFAULTS_DOC = (function(){ try { return JSON.parse(window.OMX_CONFIG_EDITOR_BOOTSTRAP.defaultsDocJson); } catch(_e){ return {dot:{}, sections:{}} } })();
      const BASE_PATH = (function(){ const m=document.querySelector('meta[name="omx-base-path"]'); const v=m&&m.getAttribute('content')||''; return v||''; })();
      function withBase(p){
        const bp = BASE_PATH||'';
        if(!bp) return p;
        if(p.startsWith('http://')||p.startsWith('https://')) return p;
        if(p.startsWith('/')) return bp + p;
        return bp + '/' + p;
      }
      // Default value hints for simple fields (shown when empty)
      const FIELD_DEFAULTS_BASE = {
        // client listener
        'client_listener.enabled': false,
        'client_listener.host': '127.0.0.1',
        'client_listener.port': 8023,
        'client_listener.max_connections': 100,
        'client_listener.connection_timeout': 30,
        // web_status
        'web_status.enable_http_api': true,
        'web_status.cors_enable': true,
        'web_status.enable_fault_injection': false,
        // logging defaults are environment-dependent; omit placeholders
        // web_status has no strong defaults for host/port
        // web_console
        'web_console.enable_ui': true,
        'web_console.session_ttl_seconds': 28800,
        'web_console.enable_probes': true,
        'web_console.probes_include_details': false,
        'web_console.use_tls': false,
        'web_console.tls_autogen': true,
        // Paths below are relative to the server working directory by default
        'web_console.static_dir': 'static',
        'web_console.template_dir': 'templates/web_console',
        // External auth UI default hints
        'auth.extauth.enabled': false,
        'auth.extauth.service': 'openmux',
        'auth.extauth.timeout': 10,
        'auth.extauth.allow_root': false,
        'auth.extauth.groups.admin_group': 'openmux_admin',
        'auth.extauth.groups.write_group': 'openmux_write',
        'auth.extauth.groups.read_group': 'openmux_read',
      };
      // Overlay doc-derived defaults for simple fields
      const FIELD_DEFAULTS = (function(){ const out = {...FIELD_DEFAULTS_BASE}; try { const m = DEFAULTS_DOC.dot||{}; Object.keys(m).forEach(k=>{ out[k]=m[k]; }); }catch(_e){} return out; })();

      function injectHelps(){
        Object.keys(FIELD_HELP).forEach(id=>{
          const el = q(id);
          if(!el) return;
          const field = el.closest('.field');
          if(!field) return;
          if(field.querySelector('.help')) return; // avoid duplicates
          const help = document.createElement('span');
          help.className='help';
          help.textContent = FIELD_HELP[id];
          field.appendChild(help);
        });
      }
      function injectDefaultHints(){
        const ensureDefaultBadge = (el, defVal)=>{
          const field = el && el.closest ? el.closest('.field') : null;
          if(!field) return;
          let badge = field.querySelector('.default-hint');
          if(!badge){ badge = document.createElement('span'); badge.className='default-hint'; field.appendChild(badge); }
          badge.textContent = `Default: ${defVal}`;
          if(el && el.tagName==='SELECT'){
            const toggle = ()=>{ badge.style.display = (el.value===''? 'inline' : 'none'); };
            toggle();
            el.addEventListener('change', toggle);
          }
        };
        Object.entries(FIELD_DEFAULTS).forEach(([id, defVal])=>{
          const el = q(id);
          if(!el) return;
          if(el.type==='text' || el.type==='number'){
            if(!el.value){ el.placeholder = String(defVal); }
          } else if(el.tagName==='SELECT'){
            ensureDefaultBadge(el, defVal);
          } else if(el.type==='checkbox'){
            ensureDefaultBadge(el, defVal);
          }
        });

        // Dynamic defaults for TLS cert/key under web_console: derive from tls_dir
        try {
          const tlsDirInput = q('web_console.tls_dir');
          const certInput = q('web_console.ssl_cert');
          const keyInput = q('web_console.ssl_key');
          const getTlsDir = ()=>{
            const v = getVal('web_console.tls_dir');
            if(v && String(v).trim().length>0) return String(v).trim();
            // fall back to defaults doc or hard default
            const docDef = FIELD_DEFAULTS['web_console.tls_dir'] || '~/.openmux/web_console';
            return String(docDef);
          };
          const joinPath = (dir, file)=>{
            if(!dir) return file;
            return dir.endsWith('/') ? (dir + file) : (dir + '/' + file);
          };
          const applyTlsHints = ()=>{
            const tdir = getTlsDir();
            if(certInput){
              const defCert = joinPath(tdir, 'server.crt');
              if(!certInput.value){ certInput.placeholder = defCert; }
              const field = certInput.closest('.field');
              if(field){
                let badge = field.querySelector('.default-hint');
                if(!badge){ badge = document.createElement('span'); badge.className='default-hint'; field.appendChild(badge); }
                badge.textContent = `Default: ${defCert}`;
              }
            }
            if(keyInput){
              const defKey = joinPath(tdir, 'server.key');
              if(!keyInput.value){ keyInput.placeholder = defKey; }
              const field = keyInput.closest('.field');
              if(field){
                let badge = field.querySelector('.default-hint');
                if(!badge){ badge = document.createElement('span'); badge.className='default-hint'; field.appendChild(badge); }
                badge.textContent = `Default: ${defKey}`;
              }
            }
          };
          applyTlsHints();
          if(tlsDirInput){ tlsDirInput.addEventListener('input', applyTlsHints); }
        } catch(_e) {}
      }
async function fetchCSRF(){ try{ const r=await fetch(withBase('/api/csrf')); if(!r.ok) return; const j=await r.json(); csrf=j.csrf; }catch(e){} }

      // Generic helpers
      function q(id){ return document.getElementById(id); }
      function setVal(id, v){ const el=q(id); if(!el) return; if(el.type==='checkbox'){ el.checked=!!v; } else if(el.tagName==='SELECT'){ el.value = (v==null?'':String(v)); } else { el.value = (v==null?'':String(v)); } }
      function getVal(id){ const el=q(id); if(!el) return undefined; if(el.type==='checkbox') return !!el.checked; if(el.type==='number') return el.value? Number(el.value) : undefined; const val = el.value; return val===''? undefined : val; }

      function toDisplay(v){ if(v===undefined||v===null||v==='') return '—'; if(typeof v==='boolean') return v?'true':'false'; return String(v); }

      const _cryptoObj = (typeof globalThis !== 'undefined' && (globalThis.crypto || globalThis.msCrypto)) ? (globalThis.crypto || globalThis.msCrypto) : null;
      const _textEncoder = (typeof TextEncoder !== 'undefined') ? new TextEncoder() : null;

      async function sha256Hex(str){
        if(!_cryptoObj || !_cryptoObj.subtle || !_textEncoder){
          throw new Error('Browser does not support Web Crypto SHA-256');
        }
        const data = _textEncoder.encode(str);
        const digest = await _cryptoObj.subtle.digest('SHA-256', data);
        return Array.from(new Uint8Array(digest)).map(b=>b.toString(16).padStart(2,'0')).join('');
      }

      function attachPasswordHelper(fieldEl, targetInput){
        const helper = document.createElement('div');
        helper.className = 'password-helper';
        const plainInput = document.createElement('input');
        plainInput.type = 'password';
        plainInput.placeholder = 'Enter plaintext password';
        plainInput.autocomplete = 'new-password';
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'mini-btn';
        btn.textContent = 'Hash password';
        const status = document.createElement('span');
        status.className = 'password-helper-status';
        helper.appendChild(plainInput);
        helper.appendChild(btn);
        helper.appendChild(status);
        fieldEl.appendChild(helper);

        const setStatus = (msg, variant)=>{
          status.textContent = msg || '';
          status.classList.remove('ok','err');
          if(!msg) return;
          if(variant==='ok') status.classList.add('ok');
          if(variant==='err') status.classList.add('err');
        };

        async function hashAndFill(){
          const plain = plainInput.value || '';
          if(!plain){
            setStatus('Enter a password to hash', 'err');
            return;
          }
          try{
            btn.disabled = true;
            setStatus('Hashing…', 'ok');
            const hex = await sha256Hex(plain);
            targetInput.value = hex;
            plainInput.value = '';
            setStatus('Password hashed and applied', 'ok');
            markDirty();
            setTimeout(()=>setStatus('', 'ok'), 3000);
          } catch(err){
            console.error('Password hash helper failed', err);
            setStatus(err && err.message ? err.message : 'Hash failed', 'err');
          } finally {
            btn.disabled = false;
          }
        }

        btn.addEventListener('click', (ev)=>{ ev.preventDefault(); hashAndFill(); });
        plainInput.addEventListener('keydown', (ev)=>{ if(ev.key === 'Enter'){ ev.preventDefault(); hashAndFill(); } });
      }
function buildTable(rootId, columns, options){ options = options||{}; const root = q(rootId); root.innerHTML=''; const wrap=document.createElement('div'); wrap.className='tbl-wrap'; const table=document.createElement('table'); table.className='config'; const thead=document.createElement('thead'); const tbody=document.createElement('tbody'); table.appendChild(thead); table.appendChild(tbody); wrap.appendChild(table); const addHost=document.createElement('div'); wrap.appendChild(addHost); root.appendChild(wrap);
  const visibleColumns = columns.filter(c=>!c.hiddenList);
  const visibleColumnCount = visibleColumns.length + 1; // include Actions column
  function renderHead(){ const tr=document.createElement('tr'); visibleColumns.forEach(c=>{const th=document.createElement('th'); const lbl=document.createElement('span'); lbl.textContent=c.label||c.key; th.appendChild(lbl); if(c.required){ const star=document.createElement('span'); star.className='req'; star.textContent=' *'; th.appendChild(star); } tr.appendChild(th);}); const thA=document.createElement('th'); thA.textContent='Actions'; tr.appendChild(thA); thead.innerHTML=''; thead.appendChild(tr); }
        renderHead();
  let rows=[]; let editingIndex=null; function setRows(newRows){
          if(activeEditorEl && wrap.contains(activeEditorEl)){
            if(typeof activeEditorCleanup==='function'){
              activeEditorCleanup({render:false});
              activeEditorCleanup=null;
            } else {
              activeEditorEl.parentNode && activeEditorEl.parentNode.removeChild(activeEditorEl);
              activeEditorEl=null;
              activeEditorValidate=null;
              activeEditorSave=null;
              editingIndex=null;
            }
          }
          rows = Array.isArray(newRows)? JSON.parse(JSON.stringify(newRows)) : [];
          if(editingIndex!=null && editingIndex>=rows.length) editingIndex=null;
          renderBody();
          try{ if(typeof options.onAfterChange==='function') options.onAfterChange(rows); markDirty(); }catch(_e){}
        }
        function renderAddBar(){ addHost.innerHTML=''; if(editingIndex===null){ const addBar=document.createElement('div'); addBar.className='row'; const bA=document.createElement('button'); bA.className='btn'; bA.type='button'; bA.textContent='Add item'; bA.addEventListener('click',()=>{
              // Open create editor without inserting a placeholder row; it will be added on save
              editRow(rows.length);
            }); addBar.appendChild(bA); addHost.appendChild(addBar); } }
        function renderBody(){ tbody.innerHTML=''; rows.forEach((row,idx)=>{ const tr=document.createElement('tr'); if(editingIndex===idx) tr.classList.add('editing'); visibleColumns.forEach(c=>{ const td=document.createElement('td'); const hasVal = !(row[c.key]===undefined || row[c.key]===null || row[c.key]===''); const v = hasVal ? row[c.key] : (c.default!==undefined ? c.default : undefined); td.textContent = toDisplay(v); td.className = (!hasVal && (c.default===undefined))? 'cell-muted':''; tr.appendChild(td); }); const tdA=document.createElement('td'); const bE=document.createElement('button'); bE.className='mini-btn edit-toggle'; bE.type='button'; const isEditingRow = (editingIndex===idx);
          bE.textContent = isEditingRow ? 'Close' : 'Edit';
          bE.addEventListener('click',()=>{
            if(editingIndex===idx && activeEditorEl){
              if(typeof activeEditorCleanup==='function'){
                const cleanup = activeEditorCleanup; activeEditorCleanup=null; cleanup();
              } else if(activeEditorEl.parentNode){
                activeEditorEl.parentNode.removeChild(activeEditorEl);
                activeEditorEl=null;
                activeEditorValidate=null;
                activeEditorSave=null;
                editingIndex=null;
                renderBody();
              }
              return;
            }
            if(activeEditorEl && typeof activeEditorValidate==='function'){
              if(!activeEditorValidate()){
                return;
              }
            }
            editRow(idx);
          });
          const bR=document.createElement('button'); bR.className='mini-btn'; bR.type='button'; bR.textContent='Remove'; bR.addEventListener('click',()=>{ try{ if(typeof options.onBeforeRemove==='function'){ const proceed = options.onBeforeRemove(rows[idx], idx); if(proceed===false){ return; } } }catch(_e){} if(editingIndex===idx && activeEditorEl && wrap.contains(activeEditorEl)){ if(typeof activeEditorCleanup==='function'){ activeEditorCleanup({render:false}); activeEditorCleanup=null; } else { activeEditorEl.parentNode && activeEditorEl.parentNode.removeChild(activeEditorEl); activeEditorEl=null; } activeEditorValidate=null; activeEditorSave=null; } rows.splice(idx,1); if(editingIndex!=null){ if(idx<editingIndex) editingIndex -= 1; else if(idx===editingIndex) editingIndex=null; } renderBody(); try{ if(typeof options.onAfterChange==='function') options.onAfterChange(rows); markDirty(); }catch(_e){} }); tdA.appendChild(bE); tdA.appendChild(bR); tr.appendChild(tdA); tbody.appendChild(tr); }); renderAddBar(); }
        function editRow(idx){ const row = rows[idx] || {}; if(typeof activeEditorCleanup==='function'){ activeEditorCleanup(); activeEditorCleanup=null; } else if(activeEditorEl && activeEditorEl.parentNode){ activeEditorEl.parentNode.removeChild(activeEditorEl); activeEditorEl=null; } activeEditorValidate=null; activeEditorSave=null; editingIndex = idx; renderBody(); const editorRow=document.createElement('tr'); editorRow.className='inline-editor-row'; const editorCell=document.createElement('td'); editorCell.colSpan = visibleColumnCount; editorRow.appendChild(editorCell); const editor=document.createElement('div'); editor.className='section inline-editor-panel'; const lg=document.createElement('div'); lg.className='legend'; const t=document.createElement('div'); t.className='title'; t.textContent = (idx===rows.length)? 'Add item' : 'Edit item'; lg.appendChild(t); const d=document.createElement('div'); d.className='desc'; d.textContent='Fields marked * are required'; lg.appendChild(d);
          const _rkinds=columns.map(c=>reloadHintFor(rootId,c.key)).filter(Boolean);
          const _rstrict=_rkinds.indexOf('full')!==-1 ? 'full' : (_rkinds[0]||null);
          if(_rstrict){
            const _rtip=_rstrict==='full' ? 'RW/RO group fields need a Full Reload; other fields apply via Soft Reload.' : (RELOAD_TIPS[_rstrict]||'');
            lg.appendChild(document.createTextNode(' ')); lg.appendChild(makeReloadHint(_rstrict,_rtip));
          }
          editor.appendChild(lg); const getters=[]; const reqChecks=[]; const errBox=document.createElement('div'); editor.appendChild(errBox);
          // Special inline row for serial 8-N-1 when editing serial_ports
          const isSerial = rootId==='serial_ports'; const isTcpInitiator = rootId==='tcp_initiator_ports'; let _protTypeInput = null;
          columns.forEach(c=>{
            // Inline group for serial settings
            if(isSerial && (c.key==='baudrate' || c.key==='bytesize' || c.key==='parity' || c.key==='stopbits')){
              // Defer handling to a single grouped row once (on baudrate)
              if(c.key!=='baudrate') return;
              const field=document.createElement('div'); field.className='field';
              const lab=document.createElement('label'); lab.textContent='Serial settings'; const star=document.createElement('span'); star.className='req'; star.textContent=' *'; lab.appendChild(star); lab.appendChild(makeReloadHint('soft', RELOAD_TIPS.soft)); field.appendChild(lab);
              // Baud (select with custom)
              const baudLabel=document.createElement('span'); baudLabel.className='subtle'; baudLabel.textContent='Baud'; field.appendChild(baudLabel);
              const baudChoices = ['', '9600','19200','38400','57600','115200','230400','460800','921600'];
              const baudSel=document.createElement('select');
              baudChoices.forEach(v=>{ const o=document.createElement('option'); o.value=v; o.textContent=(v===''? 'Custom' : v); baudSel.appendChild(o); });
              const baudCustom=document.createElement('input'); baudCustom.type='number'; baudCustom.min='1'; baudCustom.placeholder='115200';
              const existingBaud = row['baudrate'];
              if(existingBaud!=null){
                const s = String(existingBaud);
                if(baudChoices.includes(s)) { baudSel.value = s; baudCustom.style.display='none'; }
                else { baudSel.value=''; baudCustom.value=s; baudCustom.style.display='inline-block'; }
              } else {
                baudSel.value='115200'; baudCustom.style.display='none';
              }
              baudSel.addEventListener('change',()=>{
                if(baudSel.value==='') { baudCustom.style.display='inline-block'; baudCustom.focus(); }
                else { baudCustom.style.display='none'; }
              });
              field.appendChild(baudSel);
              field.appendChild(baudCustom);
              // Bytesize
              const bytesLabel=document.createElement('span'); bytesLabel.className='subtle'; bytesLabel.textContent='Data bits'; field.appendChild(bytesLabel);
              const bytes=document.createElement('select'); ['','5','6','7','8'].forEach(v=>{ const o=document.createElement('option'); o.value=v; o.textContent=v; if(String(row['bytesize']||'')===v) o.selected=true; bytes.appendChild(o); }); if(!row['bytesize']) bytes.value='8'; field.appendChild(bytes);
              // Parity (full names)
              const parityLabel=document.createElement('span'); parityLabel.className='subtle'; parityLabel.textContent='Parity'; field.appendChild(parityLabel);
              const parity=document.createElement('select');
              const parityChoices = [
                {v:'', t:''},
                {v:'N', t:'None (N)'},
                {v:'E', t:'Even (E)'},
                {v:'O', t:'Odd (O)'},
                {v:'M', t:'Mark (M)'},
                {v:'S', t:'Space (S)'}
              ];
              parityChoices.forEach(({v,t})=>{ const o=document.createElement('option'); o.value=v; o.textContent=t; if(String(row['parity']||'')===v) o.selected=true; parity.appendChild(o); });
              if(!row['parity']) parity.value='N';
              field.appendChild(parity);
              // Stopbits (friendly labels)
              const stopLabel=document.createElement('span'); stopLabel.className='subtle'; stopLabel.textContent='Stop bits'; field.appendChild(stopLabel);
              const stop=document.createElement('select'); const stopChoices=[{v:'',t:''},{v:'1',t:'1 stop bit'},{v:'2',t:'2 stop bits'}]; stopChoices.forEach(({v,t})=>{ const o=document.createElement('option'); o.value=v; o.textContent=t; if(String(row['stopbits']||'')===v) o.selected=true; stop.appendChild(o); }); if(!row['stopbits']) stop.value='1'; field.appendChild(stop);
              // Group help
              const ghelp=document.createElement('span'); ghelp.className='help'; ghelp.textContent='Baud/Data bits/Parity/Stop bits. Defaults to 115200 8-N-1. Use "Custom" to enter a non-listed baud.'; field.appendChild(ghelp);
              editor.appendChild(field);
              // Collectors and required checks for the grouped fields
              getters.push(()=>['baudrate', (baudSel.value!==''? Number(baudSel.value): (baudCustom.value!==''? Number(baudCustom.value):undefined))]);
              getters.push(()=>['bytesize', bytes.value!==''? Number(bytes.value):undefined]);
              getters.push(()=>['parity', parity.value||undefined]);
              getters.push(()=>['stopbits', stop.value!==''? Number(stop.value):undefined]);
              reqChecks.push(()=>({key:'baudrate', ok: (baudSel.value!=='' || baudCustom.value!=='')}));
              reqChecks.push(()=>({key:'bytesize', ok: bytes.value!==''}));
              reqChecks.push(()=>({key:'parity', ok: parity.value!==''}));
              reqChecks.push(()=>({key:'stopbits', ok: stop.value!==''}));
              return; // Skip default handling for grouped fields
            }
            // Default field rendering
            const lab=document.createElement('label'); lab.textContent=c.label||c.key; if(c.required){ const star=document.createElement('span'); star.className='req'; star.textContent=' *'; lab.appendChild(star); } const _rr=reloadHintFor(rootId, c.key); if(_rr){ lab.appendChild(makeReloadHint(_rr, RELOAD_TIPS[_rr])); } const field=document.createElement('div'); field.className='field'; field.appendChild(lab); let input; if(c.type==='boolean'){ input=document.createElement('input'); input.type='checkbox'; if(row[c.key]!==undefined && row[c.key]!==null){ input.checked=!!row[c.key]; } else if(c.default!==undefined){ input.checked=!!c.default; } else { input.checked=false; } } else if(c.type==='enum'){ input=document.createElement('select'); const blank=document.createElement('option'); blank.value=''; blank.textContent=''; input.appendChild(blank); (c.enum||[]).forEach(v=>{ const o=document.createElement('option'); o.value=String(v); o.textContent=String(v); if(String(row[c.key])===String(v)) o.selected=true; input.appendChild(o); }); } else if(c.type==='number'||c.type==='integer'){ input=document.createElement('input'); input.type='number'; if(c.min!==undefined) input.min=String(c.min); if(c.max!==undefined) input.max=String(c.max); if(c.step!==undefined) input.step=String(c.step); if(row[c.key]!==undefined && row[c.key]!==null) input.value=String(row[c.key]); if((row[c.key]===undefined || row[c.key]===null) && c.default!==undefined){ input.placeholder = String(c.default); } } else if(c.type==='array-string'){ input=document.createElement('input'); input.type='text'; input.placeholder='comma,separated,values'; if(Array.isArray(row[c.key])) input.value=row[c.key].join(','); } else { input=document.createElement('input'); input.type='text'; if(row[c.key]!==undefined && row[c.key]!==null) input.value=String(row[c.key]); if((row[c.key]===undefined || row[c.key]===null) && c.default!==undefined){ input.placeholder = String(c.default); } }
            if(c.placeholder){ input.placeholder=c.placeholder; }
            field.appendChild(input);
            const needsPasswordHelper = (rootId==='auth.users' && c.key==='password_hash');
            if(needsPasswordHelper){
              try{ attachPasswordHelper(field, input); }catch(_e){ /* non-fatal */ }
            }
            if(c.help){ const help=document.createElement('span'); help.className='help'; help.textContent=c.help; field.appendChild(help);} if(c.default!==undefined){ const def=document.createElement('span'); def.className='default-hint'; const dval = (typeof c.default==='boolean') ? (c.default? 'true':'false') : String(c.default); def.textContent = `Default: ${dval}`; field.appendChild(def); } if(isTcpInitiator){ const _pg={protocol_telnet_negotiation:['plain'],protocol_console_name:['conserver'],protocol_username:['conserver','openmux'],protocol_password:['conserver','openmux'],protocol_remote_port:['openmux'],protocol_api_key:['openmux']}; if(_pg[c.key]) field.dataset.protocolGroup=_pg[c.key].join(' '); if(c.key==='protocol_type') _protTypeInput=input; } editor.appendChild(field); getters.push(()=>{ let v; if(c.type==='boolean') v=!!input.checked; else if(c.type==='number'||c.type==='integer') v=input.value!==''? Number(input.value):undefined; else if(c.type==='enum') v=input.value||undefined; else if(c.type==='array-string') v=input.value? input.value.split(',').map(s=>s.trim()).filter(s=>s.length>0):[]; else v=input.value||undefined; return [c.key, v]; }); if(c.required){ reqChecks.push(()=>{ const val = (c.type==='boolean')? !!input.checked : (c.type==='number'||c.type==='integer')? (input.value!=='' ? true : false) : (Array.isArray(input.value)? input.value.length>0 : (input.value && input.value.trim().length>0)); return {key:c.key, ok: !!val}; }); }
          });
          if(isTcpInitiator && _protTypeInput){ const _upd=function(){ const ptype=_protTypeInput.value||'plain'; editor.querySelectorAll('[data-protocol-group]').forEach(function(f){ f.style.display=f.dataset.protocolGroup.split(' ').includes(ptype)?'':'none'; }); }; _protTypeInput.addEventListener('change',_upd); _upd(); }
          const bar=document.createElement('div'); bar.className='row'; const bS=document.createElement('button'); bS.className='btn'; bS.textContent='Save item'; const bC=document.createElement('button'); bC.className='btn'; bC.textContent='Cancel'; bar.appendChild(bS); bar.appendChild(bC); editor.appendChild(bar); editorCell.appendChild(editor); const anchor = tbody.children[idx] || tbody.lastElementChild; if(anchor){ anchor.insertAdjacentElement('afterend', editorRow); } else { tbody.appendChild(editorRow); } try{ editor.scrollIntoView({behavior:'smooth', block:'nearest'}); }catch(_e){}
          activeEditorEl = editorRow;
          activeEditorCleanup = (cleanupOpts)=>{ if(editorRow.parentNode) editorRow.parentNode.removeChild(editorRow); if(activeEditorEl===editorRow){ activeEditorEl=null; } activeEditorValidate=null; activeEditorSave=null; editingIndex=null; if(!cleanupOpts || cleanupOpts.render !== false){ renderBody(); } };
          activeEditorValidate = ()=>{ const missing = reqChecks.map(f=>f()).filter(r=>!r.ok).map(r=>r.key); if(missing.length>0){ errBox.className='err'; errBox.textContent = 'Missing required: ' + missing.join(', '); try{ editor.scrollIntoView({behavior:'smooth', block:'nearest'}); }catch(_e){} return false; } errBox.textContent=''; return true; };
          activeEditorSave = ()=>{ if(!activeEditorValidate || activeEditorValidate()){ const obj={}; getters.forEach(g=>{ const [k,v]=g(); if(v!==undefined) obj[k]=v; }); rows[idx]=obj; const cleanup = activeEditorCleanup; activeEditorCleanup=null; if(typeof cleanup==='function'){ cleanup(); } else if(editorRow.parentNode){ editorRow.parentNode.removeChild(editorRow); editingIndex=null; renderBody(); } try{ if(typeof options.onAfterChange==='function') options.onAfterChange(rows); markDirty(); }catch(_e){} return true; } return false; };
          bS.addEventListener('click',()=>{ activeEditorSave && activeEditorSave(); }); bC.addEventListener('click',()=>{ if(typeof activeEditorCleanup==='function'){ activeEditorCleanup(); activeEditorCleanup=null; } else if(editorRow.parentNode){ editorRow.parentNode.removeChild(editorRow); editingIndex=null; renderBody(); } activeEditorValidate=null; activeEditorSave=null; }); }
        renderAddBar();
        wrap._get = ()=> rows;
        wrap._set = setRows;
        return wrap;
      }

      // Build static UI behavior
      const tables = {};
      // Help text for table columns (used in edit dialogs)
      const COLUMN_HELP = {
        'auth.users': {
          username: 'Login username for HTTP basic-auth.',
          password_hash: 'SHA-256 hex password hash. Use the Hash password helper to derive it client-side without leaving the browser.',
          permissions: 'Role for future fine-grained access control.',
          groups: 'Console groups this user belongs to, used for per-console read_write_groups/read_only_groups access control.'
        },
        'auth.api_keys': {
          name: 'Human-friendly name for this API key.',
          key: 'The API key string presented by clients.',
          permissions: 'Access level for this API key.',
          groups: 'Console groups this API key belongs to, used for per-console read_write_groups/read_only_groups access control.'
        },
        'auth.public_keys': {
          key_id: 'Identifier for this public key (referenced by peers).',
          public_key: 'OpenSSH-format ed25519 public key.',
          username: 'Associate this key with a username (optional).',
          allowed_uses: 'Restrict usage of this key to client and/or muxcon.'
        },
        'serial_ports': {
          name: 'Logical port name used by clients.',
          description: 'Optional description shown in UIs.',
          device: 'Serial device path, e.g., /dev/ttyUSB0.',
          baudrate: 'Bits per second. Use Custom for non-standard rates.',
          bytesize: 'Number of data bits per character.',
          parity: 'Parity mode: None/Even/Odd/Mark/Space.',
          stopbits: 'Number of stop bits.',
          timeout: 'Read timeout in seconds (0 for non-blocking).',
          flow_control: 'Hardware/software flow control (none/rtscts/dsrdtr/xonxoff).',
          dtr: 'Initial DTR line state.',
          rts: 'Initial RTS line state.',
          max_read_write_users: 'How many users may write at once: one = 1, multiple = unlimited, none = no driver (admin included).',
          scrollback_size: 'Bytes of recent output to buffer for scrollback replay (0 = disabled). Clients request replay with ?scrollback=1.',
          read_write_groups: 'Console groups granted read-write access. Empty = open to all authenticated users.',
          read_only_groups: 'Console groups granted read-only access (never promoted to read-write). Empty = open to all authenticated users.'
        },
        'loopback_ports': {
          name: 'Logical name for the software loopback port.',
          description: 'Optional description for the loopback.',
          buffer_size: 'Size of the internal buffer (bytes).',
          echo_delay: 'Artificial echo delay in seconds.',
          sanitize_control: 'Replace non-printable control characters with visible placeholders (e.g., show ^C instead of raw control bytes); helps prevent terminal glitches when testing.',
          max_read_write_users: 'How many users may write at once: one = 1 (default), multiple = unlimited, none = no driver (admin included).',
          scrollback_size: 'Bytes of recent output to buffer for scrollback replay (0 = disabled). Clients request replay with ?scrollback=1.',
          read_write_groups: 'Console groups granted read-write access. Empty = open to all authenticated users.',
          read_only_groups: 'Console groups granted read-only access (never promoted to read-write). Empty = open to all authenticated users.'
        },
        'command_ports': {
          name: 'Logical name for this command-backed port.',
          description: 'Optional description for the command port.',
          command: 'Command or executable to run for the session.',
          shell: 'Run via shell (true) or exec directly (false).',
          cwd: 'Working directory for the command.',
          max_read_write_users: 'How many users may write at once: one = 1 (default), multiple = unlimited, none = no driver (admin included).',
          interactive: 'Allocate interactive TTY-like behavior.',
          always_buffer: 'Buffer early output until first client connects.',
          scrollback_size: 'Bytes of recent output to buffer for scrollback replay (0 = disabled). Clients request replay with ?scrollback=1.',
          read_write_groups: 'Console groups granted read-write access. Empty = open to all authenticated users.',
          read_only_groups: 'Console groups granted read-only access (never promoted to read-write). Empty = open to all authenticated users.'
        },
        'tcp_initiator_ports': {
          name: 'Logical name for this TCP initiator port.',
          description: 'Optional description for the TCP initiator port.',
          host: 'Remote host/IP to connect to.',
          port: 'Remote TCP port number. Conserver default: 782.',
          use_tls: 'Enable TLS for the outbound connection.',
          ssl_verify: 'Verify TLS certificates when TLS is enabled.',
          timeout: 'Connection timeout in seconds.',
          auto_reconnect: 'Automatically reconnect when the session drops.',
          reconnect_delay: 'Seconds to wait before the next reconnect attempt.',
          enable_batching: 'Buffer small writes briefly before sending to reduce syscall overhead.',
          batch_size: 'Maximum bytes to accumulate before flushing a batch.',
          batch_timeout: 'Maximum time to wait before flushing a batch (seconds).',
          enabled: 'Temporarily disable this port without deleting it.',
          connect_on_demand: 'Only connect to the remote when a user opens this port. Useful for expensive or shared connections (e.g. conserver consoles).',
          disconnect_when_idle: 'Disconnect from the remote when the last user leaves.',
          idle_disconnect_delay: 'Seconds to wait after the last user disconnects before tearing down the remote connection.',
          protocol_type: 'Protocol to use for this connection. "plain" = raw TCP (default). "conserver" = conserver handshake. "openmux" = OpenMux auth + port selection.',
          protocol_telnet_negotiation: 'How to handle telnet command sequences in plain mode. "strip" silently absorbs them. Default: none.',
          protocol_console_name: 'Console name to attach to on the conserver server.',
          protocol_username: 'Username for authentication (conserver or openmux).',
          protocol_password: 'Password for authentication. Leave blank if the server uses passwordless / PAM auth.',
          protocol_remote_port: 'Port name on the remote OpenMux server to connect to.',
          protocol_api_key: 'API key for OpenMux authentication (alternative to username + password).',
          scrollback_size: 'Bytes of recent output to buffer for scrollback replay (0 = disabled). Clients request replay with ?scrollback=1.',
          read_write_groups: 'Console groups granted read-write access. Empty = open to all authenticated users.',
          read_only_groups: 'Console groups granted read-only access (never promoted to read-write). Empty = open to all authenticated users.'
        },
        'telnet_listener': {
          name: 'Listener identifier used in logs and client banners.',
          bind_host: 'Interface or IP to bind. Use 0.0.0.0 for all IPv4 interfaces.',
          bind_port: 'TCP port that telnet clients will connect to (1-65535).',
          target: 'OpenMux port to attach (local::name, server_id::name, or bare name), or "*" for a port-selection menu after login.',
          read_only: 'Drop client keystrokes and stream port output only.',
          enabled: 'Temporarily disable this listener without deleting it.',
          require_auth: 'Require a login/password prompt before attaching. Recommended when target is "*". Also enables the "<user>+<port>" / "<user>:<port>" login shortcut.',
          acl: 'Comma-separated IP or CIDR allowlist. Leave blank to permit all clients.'
        },
        'ssh_listener': {
          name: 'Listener identifier used in logs and client banners.',
          bind_host: 'Interface or IP to bind. Use 0.0.0.0 for all IPv4 interfaces.',
          bind_port: 'TCP port that SSH clients will connect to (1-65535).',
          target: 'OpenMux port to attach (local::name, server_id::name, or bare name), or "*" for a port-selection menu after login.',
          read_only: 'Drop client keystrokes and stream port output only.',
          enabled: 'Temporarily disable this listener without deleting it.',
          require_auth: 'Require password or public-key authentication before attaching. Also enables the "<user>+<port>" / "<user>:<port>" login shortcut in the SSH username field.',
          acl: 'Comma-separated IP or CIDR allowlist. Leave blank to permit all clients.'
        },
        'muxcon.listeners': {
          enabled: 'Enable or disable this listener entry.',
          host: 'Bind address for incoming muxcon connections.',
          port: 'TCP port to listen on.',
          use_tls: 'Enable TLS for this listener (HTTPS-like).',
          ssl_cert: 'Path to server certificate (PEM).',
          ssl_key: 'Path to private key (PEM).',
          ssl_ca_cert: 'CA certificate for client verification.',
          require_client_cert: 'Require client certificates (mTLS).',
          tls_autogen: 'Autogenerate self-signed certs if missing.',
          tls_dir: 'Directory for generated or stored TLS files.',
          tls_known_peers_path: 'Known-peers file for TOFU/pinning.',
          interface: 'Bind to a specific network interface name.',
          fwmark: 'Linux fwmark for policy routing (advanced).',
          path_pref: 'Preference value for path selection (higher is preferred).',
          path_group: 'Group name to allow multi-path within a group.'
        },
        'muxcon.initiators': {
          host: 'Remote muxcon peer address to connect to.',
          port: 'Remote muxcon peer TCP port.',
          share_ports: 'Ports to share with the remote peer (advertise).',
          accept_ports: 'Ports you will accept from the remote peer.',
          request_ports: 'Ports to request from the remote peer.',
          use_tls: 'Use TLS for the outbound connection.',
          ssl_verify: 'Verify the server certificate.',
          ssl_ca_cert: 'CA certificate to trust when verifying.',
          ssl_cert: 'Client certificate for mTLS.',
          ssl_key: 'Client private key for mTLS.',
          server_hostname: 'Override SNI/server hostname for TLS.',
          tls_pin_fingerprint: 'Pin to a certificate fingerprint.',
          tls_tofu: 'Trust-On-First-Use: remember initial cert.',
          bind_host: 'Local address to bind the socket.',
          bind_port: 'Local port to bind (0 for automatic).',
          source_ip: 'Source IP for the connection (advanced).',
          interface: 'Bind to a specific network interface.',
          fwmark: 'Linux fwmark for policy routing (advanced).',
          retry_backoff_initial: 'Initial reconnect backoff (seconds).',
          retry_backoff_max: 'Maximum reconnect backoff (seconds).',
          retry_short_session_sec: 'Treat sessions shorter than this as failures.'
        }
        ,
        'web_console.plugins': {
          module: 'Python import path of a web plugin module to load, e.g., openmux.server.web_plugins.config_editor.'
        }
        ,
        'muxcon.public_keys': {
          key_id: 'Key identifier for this muxcon public key.',
          public_key: 'Ed25519 public key (OpenSSH format or base64:...).',
          advertise_filters: 'Default advertise filters for peers authenticated with this key.',
          accept_filters: 'Default accept filters for peers authenticated with this key.'
        }
        ,
        'port_actions.action_ports': {
          action_id: 'Action script id, e.g. echo_probe, slow_noop, confirm_probe, setup_wizard.',
          ports: 'Comma-separated port names granted this action, or * for every port.'
        }
      };

      function annotateColumnsWithHelp(rootId, cols){
        const map = COLUMN_HELP[rootId] || {};
        return cols.map(c=>{ if(!c.help && map[c.key]){ c = {...c, help: map[c.key]}; } return c; });
      }
      // Default value hints for table columns per section
      const COLUMN_DEFAULTS_BASE = {
        'serial_ports': { baudrate: 115200, bytesize: 8, parity: 'N', stopbits: 1 },
        'loopback_ports': { max_read_write_users: 'one', echo_delay: 0.0, buffer_size: 1024, sanitize_control: true },
        'command_ports': { max_read_write_users: 'one', shell: false },
        'tcp_initiator_ports': { enabled: true, use_tls: false, ssl_verify: true, timeout: 10.0, auto_reconnect: true, reconnect_delay: 5.0, enable_batching: true, batch_size: 1024, batch_timeout: 0.015, protocol_type: 'plain', protocol_telnet_negotiation: 'none', connect_on_demand: false, disconnect_when_idle: false, idle_disconnect_delay: 30.0 },
        'telnet_listener': { bind_host: '0.0.0.0', read_only: false, enabled: true, require_auth: false },
        'ssh_listener': { bind_host: '0.0.0.0', read_only: false, enabled: true, require_auth: true },
        // Default-safe: TLS on by default for muxcon listeners
        'muxcon.listeners': { use_tls: true, require_client_cert: false, tls_autogen: true },
        'muxcon.initiators': {
          // Default-safe: TLS on by default for initiators
          use_tls: true,
          ssl_verify: true,
          retry_backoff_initial: 2.0,
          retry_backoff_max: 30.0,
          retry_short_session_sec: 5.0,
        }
      };
      // Overlay doc-derived section defaults
      const COLUMN_DEFAULTS = (function(){ const out = JSON.parse(JSON.stringify(COLUMN_DEFAULTS_BASE)); try { const sect = DEFAULTS_DOC.sections||{}; Object.keys(sect).forEach(sec=>{ out[sec] = {...(out[sec]||{}), ...(sect[sec]||{})}; }); }catch(_e){} return out; })();
      function annotateColumnsWithDefaults(rootId, cols){
        const map = COLUMN_DEFAULTS[rootId] || {};
        return cols.map(c=>{
          if(map.hasOwnProperty(c.key) && c.default===undefined){
            c = {...c, default: map[c.key]};
          }
          return c;
        });
      }

      // Reload requirements per config field, derived from the reload code paths
      // (main.py reload_adapters_soft/full, SIGHUP handler, and start()-only reads).
      //   'soft'    - applied by Soft Reload (SIGHUP / the Soft Reload button)
      //   'full'    - applied only by Full Reload (SIGUSR1 / the Full Reload button)
      //   'restart' - applied only on process restart
      //   'sighup'  - applied only by kill -HUP (not by either editor button)
      //   'live'    - picked up on use; no reload needed
      // Simple fields are keyed by input id; table columns by "<rootId>.<key>".
      // Per-field marks win over the section header badges.
      const RELOAD_REQUIREMENTS = {
        // server.id/description are re-read by live adapters on full reload only
        'server.id': 'full',
        'server.description': 'full',
        // control socket and PID file are created at process start
        'server.control_socket': 'restart',
        'server.pidfile': 'restart',
        // console access posture (issue #58): security.yaml is re-read on a soft
        // reload; the ladder picks the value up from the next connection
        'security.access_default': 'soft',
        // log level is re-applied by the SIGHUP signal handler only
        'logging.level': 'sighup',
        // log handler set-up (console/file/rotation) happens at process start
        'logging.console': 'restart',
        'logging.file': 'restart',
        'logging.log_dir': 'restart',
        'logging.max_log_size': 'restart',
        'logging.log_backup_count': 'restart',
        // action catalog is re-checked on every use; no reload needed
        'port_actions.actions_dir': 'live',
        // Web console UI text is hot-applied on a soft reload (realm is read
        // per request, the MOTDs on each render); the rest of the web_console
        // section needs a full reload, so the SOFT marks override the section
        // badge for these three fields.
        'web_console.realm': 'soft',
        'web_console.motd': 'soft',
        'web_console.logged_in_motd': 'soft',
      };
      // Tables whose per-row fields are applied by Soft Reload reconcile
      const SOFT_RELOAD_TABLE_ROOTS = new Set([
        'serial_ports','loopback_ports','command_ports','tcp_initiator_ports',
        'telnet_listener','ssh_listener','muxcon.listeners','muxcon.initiators','muxcon.public_keys',
      ]);
      // Access-group fields are NOT included in any port reconcile diff, so they
      // only take effect when the port is re-created (Full Reload).
      const FULL_RELOAD_ONLY_COLUMNS = new Set(['read_write_groups', 'read_only_groups']);
      const RELOAD_TIPS = {
        soft: 'Applied by Soft Reload (or a Full Reload).',
        full: 'Needs a Full Reload. A Soft Reload does not update this.',
        restart: 'Needs a process restart.',
        sighup: 'Needs kill -HUP (SIGHUP). The editor reload buttons do not change this.',
        live: 'Applied on use; no reload needed.',
      };
      function reloadHintFor(rootId, key){
        const v = RELOAD_REQUIREMENTS[rootId + '.' + key];
        if(v) return v;
        if(SOFT_RELOAD_TABLE_ROOTS.has(rootId)) return FULL_RELOAD_ONLY_COLUMNS.has(key) ? 'full' : 'soft';
        return null;
      }
      function makeReloadHint(kind, tip){
        const s = document.createElement('span');
        s.className = 'reload-hint reload-hint-' + kind;
        s.textContent = kind; /* CSS renders it lowercase */
        if(tip) s.title = tip;
        return s;
      }
      function injectReloadHints(){
        Object.keys(RELOAD_REQUIREMENTS).forEach(id=>{
          const el = q(id); if(!el) return;
          const field = el.closest('.field'); if(!field) return;
          const lab = field.querySelector('label'); if(!lab) return;
          if(lab.querySelector('.reload-hint')) return;
          const kind = RELOAD_REQUIREMENTS[id];
          lab.appendChild(makeReloadHint(kind, RELOAD_TIPS[kind]));
        });
      }
      function initStatic(){
        const CONFIG_EDITOR_MODULE = 'openmux.server.web_plugins.config_editor';
        const advisory = document.getElementById('advisory');
        function setAdvisory(msg){
          if(!advisory) return;
          advisory.innerHTML = '';
          if(msg){ const el=document.createElement('div'); el.className='warn'; el.textContent=msg; advisory.appendChild(el); }
        }
        // Client listener enable/disable wiring
        function updateClientListenerUI(){
          const enabled = !!getVal('client_listener.enabled');
          const ids = ['client_listener.host','client_listener.port','client_listener.max_connections','client_listener.connection_timeout'];
          ids.forEach(id=>{ const el = q(id); if(el){ el.disabled = !enabled; if(!enabled){ /* keep placeholders visible while disabled */ } } });
        }
        const clEnable = q('client_listener.enabled');
        if(clEnable){
          clEnable.addEventListener('change', ()=>{
            if(!clEnable.checked){
              const ok = confirm('Disabling the client listener will remove the entire client_listener block from the config and clear its options. Continue?');
              if(!ok){ clEnable.checked = true; updateClientListenerUI(); return; }
              // Clear values when disabling
              ['client_listener.host','client_listener.port','client_listener.max_connections','client_listener.connection_timeout'].forEach(id=>{ const el=q(id); if(el){ if(el.type==='checkbox'){ el.checked=false; } else { el.value=''; } } });
            }
            updateClientListenerUI();
          });
          // Initialize state on load
          updateClientListenerUI();
        }
        function updateAdvisory(){
          const uiEnabled = !!getVal('web_console.enable_ui');
          const pluginsTable = tables['web_console.plugins'];
          const rows = pluginsTable && pluginsTable._get ? pluginsTable._get() : [];
          const hasEditor = Array.isArray(rows) && rows.some(r=>r && String(r.module||'')===CONFIG_EDITOR_MODULE);
          if(!uiEnabled){
            setAdvisory('Web console UI is disabled; the Config Editor will not be accessible.');
          } else if(!hasEditor){
            setAdvisory('Config Editor plugin is not configured; the Config page will not be available.');
          } else {
            setAdvisory('');
          }
        }
        tables['auth.users'] = buildTable('auth.users', annotateColumnsWithDefaults('auth.users', annotateColumnsWithHelp('auth.users', [
          {key:'username', label:'Username', type:'string', required:true},
          {key:'password_hash', label:'Password hash', type:'string', required:true, hiddenList:true},
          {key:'permissions', label:'Permissions', type:'enum', enum:['admin','read-write','read-only']},
          {key:'groups', label:'Console groups', type:'array-string', hiddenList:true}
        ])));
        tables['auth.api_keys'] = buildTable('auth.api_keys', annotateColumnsWithDefaults('auth.api_keys', annotateColumnsWithHelp('auth.api_keys', [
          {key:'name', label:'Name', type:'string'},
          {key:'key', label:'Key', type:'string', required:true, hiddenList:true},
          {key:'permissions', label:'Permissions', type:'enum', enum:['admin','read-write','read-only']},
          {key:'groups', label:'Console groups', type:'array-string', hiddenList:true}
        ])));
        tables['auth.public_keys'] = buildTable('auth.public_keys', annotateColumnsWithDefaults('auth.public_keys', annotateColumnsWithHelp('auth.public_keys', [
          {key:'key_id', label:'Key ID', type:'string', required:true},
          {key:'public_key', label:'Public key', type:'string', required:true},
          {key:'username', label:'Username (optional)', type:'string'},
          {key:'allowed_uses', label:'Allowed uses', type:'array-string'}
        ])));

        tables['serial_ports'] = buildTable('serial_ports', annotateColumnsWithDefaults('serial_ports', annotateColumnsWithHelp('serial_ports', [
          {key:'name', label:'Name', type:'string', required:true},
          {key:'description', label:'Description', type:'string'},
          {key:'device', label:'Device', type:'string', required:true},
          {key:'baudrate', label:'Baud', type:'integer', min:1, required:true},
          {key:'bytesize', label:'Data bits', type:'enum', enum:[5,6,7,8], required:true},
          {key:'parity', label:'Parity', type:'enum', enum:['N','E','O','M','S'], required:true},
          {key:'stopbits', label:'Stop bits', type:'enum', enum:[1,2], required:true},
          {key:'timeout', label:'Timeout', type:'number', min:0, step:0.1},
          {key:'flow_control', label:'Flow', type:'enum', enum:['none','rtscts','dsrdtr','xonxoff']},
          {key:'dtr', label:'DTR', type:'boolean'},
          {key:'rts', label:'RTS', type:'boolean'},
          {key:'max_read_write_users', label:'Write slots', type:'enum', enum:['one','multiple','none']},
          {key:'scrollback_size', label:'Scrollback (bytes)', type:'integer', min:0},
          {key:'read_write_groups', label:'RW groups', type:'array-string', hiddenList:true},
          {key:'read_only_groups', label:'RO groups', type:'array-string', hiddenList:true}
        ])));

        tables['loopback_ports'] = buildTable('loopback_ports', annotateColumnsWithDefaults('loopback_ports', annotateColumnsWithHelp('loopback_ports', [
          {key:'name', label:'Name', type:'string', required:true},
          {key:'description', label:'Description', type:'string'},
          {key:'buffer_size', label:'Buffer size', type:'integer', min:1},
          {key:'echo_delay', label:'Echo delay', type:'number', min:0, step:0.1},
          {key:'sanitize_control', label:'Sanitize control', type:'boolean'},
          {key:'max_read_write_users', label:'Write slots', type:'enum', enum:['one','multiple','none']},
          {key:'scrollback_size', label:'Scrollback (bytes)', type:'integer', min:0},
          {key:'read_write_groups', label:'RW groups', type:'array-string', hiddenList:true},
          {key:'read_only_groups', label:'RO groups', type:'array-string', hiddenList:true}
        ])));

        tables['command_ports'] = buildTable('command_ports', annotateColumnsWithDefaults('command_ports', annotateColumnsWithHelp('command_ports', [
          {key:'name', label:'Name', type:'string', required:true},
          {key:'description', label:'Description', type:'string'},
          {key:'command', label:'Command', type:'string', required:true},
          {key:'shell', label:'Shell', type:'boolean'},
          {key:'cwd', label:'CWD', type:'string'},
          {key:'max_read_write_users', label:'Write slots', type:'enum', enum:['one','multiple','none']},
          {key:'interactive', label:'Interactive', type:'boolean'},
          {key:'always_buffer', label:'Always buffer', type:'boolean'},
          {key:'scrollback_size', label:'Scrollback (bytes)', type:'integer', min:0},
          {key:'read_write_groups', label:'RW groups', type:'array-string', hiddenList:true},
          {key:'read_only_groups', label:'RO groups', type:'array-string', hiddenList:true}
        ])));

        tables['tcp_initiator_ports'] = buildTable('tcp_initiator_ports', annotateColumnsWithDefaults('tcp_initiator_ports', annotateColumnsWithHelp('tcp_initiator_ports', [
          {key:'name', label:'Name', type:'string', required:true},
          {key:'description', label:'Description', type:'string'},
          {key:'enabled', label:'Enabled', type:'boolean'},
          {key:'host', label:'Host', type:'string', required:true},
          {key:'port', label:'Port', type:'integer', min:1, max:65535, required:true},
          {key:'use_tls', label:'Use TLS', type:'boolean'},
          {key:'ssl_verify', label:'Verify TLS', type:'boolean'},
          {key:'timeout', label:'Timeout (s)', type:'number', min:0, step:0.1},
          {key:'auto_reconnect', label:'Auto reconnect', type:'boolean'},
          {key:'reconnect_delay', label:'Reconnect delay (s)', type:'number', min:0, step:0.1},
          {key:'enable_batching', label:'Batch writes', type:'boolean'},
          {key:'batch_size', label:'Batch size (bytes)', type:'integer', min:1},
          {key:'batch_timeout', label:'Batch timeout (s)', type:'number', min:0, step:0.001},
          {key:'connect_on_demand', label:'Connect on demand', type:'boolean'},
          {key:'disconnect_when_idle', label:'Disconnect when idle', type:'boolean', hiddenList:true},
          {key:'idle_disconnect_delay', label:'Idle disconnect (s)', type:'number', min:0, step:1, hiddenList:true},
          {key:'protocol_type', label:'Protocol', type:'enum', enum:['plain','conserver','openmux'], default:'plain'},
          {key:'protocol_telnet_negotiation', label:'Telnet negotiation', type:'enum', enum:['none','strip'], hiddenList:true},
          {key:'protocol_console_name', label:'Console name', type:'string', hiddenList:true},
          {key:'protocol_username', label:'Username', type:'string', hiddenList:true},
          {key:'protocol_password', label:'Password', type:'string', hiddenList:true},
          {key:'protocol_remote_port', label:'Remote port', type:'string', hiddenList:true},
          {key:'protocol_api_key', label:'API key', type:'string', hiddenList:true},
          {key:'scrollback_size', label:'Scrollback (bytes)', type:'integer', min:0},
          {key:'read_write_groups', label:'RW groups', type:'array-string', hiddenList:true},
          {key:'read_only_groups', label:'RO groups', type:'array-string', hiddenList:true}
        ])));
        tables['telnet_listener'] = buildTable('telnet_listener', annotateColumnsWithDefaults('telnet_listener', annotateColumnsWithHelp('telnet_listener', [
          {key:'name', label:'Name', type:'string', required:true},
          {key:'bind_host', label:'Bind host', type:'string', placeholder:'0.0.0.0'},
          {key:'bind_port', label:'Port', type:'integer', min:1, max:65535, required:true},
          {key:'target', label:'Target port', type:'string', required:true},
          {key:'read_only', label:'Read-only', type:'boolean'},
          {key:'enabled', label:'Enabled', type:'boolean'},
          {key:'require_auth', label:'Require auth', type:'boolean'},
          {key:'acl', label:'ACL (IP/CIDR, comma)', type:'array-string'}
        ])));

        tables['ssh_listener'] = buildTable('ssh_listener', annotateColumnsWithDefaults('ssh_listener', annotateColumnsWithHelp('ssh_listener', [
          {key:'name', label:'Name', type:'string', required:true},
          {key:'bind_host', label:'Bind host', type:'string', placeholder:'0.0.0.0'},
          {key:'bind_port', label:'Port', type:'integer', min:1, max:65535, required:true},
          {key:'target', label:'Target port', type:'string', required:true},
          {key:'read_only', label:'Read-only', type:'boolean'},
          {key:'enabled', label:'Enabled', type:'boolean'},
          {key:'require_auth', label:'Require auth', type:'boolean'},
          {key:'acl', label:'ACL (IP/CIDR, comma)', type:'array-string'}
        ])));

        tables['muxcon.listeners'] = buildTable('muxcon.listeners', annotateColumnsWithDefaults('muxcon.listeners', annotateColumnsWithHelp('muxcon.listeners', [
          {key:'enabled', label:'Enabled', type:'boolean', required:true},
          {key:'host', label:'Host', type:'string', required:true},
          {key:'port', label:'Port', type:'integer', min:1, max:65535, required:true},
          {key:'use_tls', label:'TLS', type:'boolean'},
          {key:'ssl_cert', label:'SSL cert', type:'string'},
          {key:'ssl_key', label:'SSL key', type:'string'},
          {key:'ssl_ca_cert', label:'CA cert', type:'string'},
          {key:'require_client_cert', label:'Mutual TLS', type:'boolean'},
          {key:'tls_autogen', label:'Autogen', type:'boolean'},
          {key:'tls_dir', label:'TLS dir', type:'string'},
          {key:'tls_known_peers_path', label:'Known peers', type:'string'},
          {key:'interface', label:'Interface', type:'string'},
          {key:'fwmark', label:'fwmark', type:'integer'},
          {key:'path_pref', label:'Path pref', type:'integer'},
          {key:'path_group', label:'Path group', type:'string'}
        ])));

        tables['muxcon.initiators'] = buildTable('muxcon.initiators', annotateColumnsWithDefaults('muxcon.initiators', annotateColumnsWithHelp('muxcon.initiators', [
          {key:'host', label:'Host', type:'string', required:true},
          {key:'port', label:'Port', type:'integer', min:1, max:65535, required:true},
          {key:'share_ports', label:'Share ports', type:'array-string'},
          {key:'accept_ports', label:'Accept ports', type:'array-string'},
          {key:'request_ports', label:'Request ports', type:'array-string'},
          {key:'use_tls', label:'TLS', type:'boolean'},
          {key:'ssl_verify', label:'Verify', type:'boolean'},
          {key:'ssl_ca_cert', label:'CA cert', type:'string'},
          {key:'ssl_cert', label:'SSL cert', type:'string'},
          {key:'ssl_key', label:'SSL key', type:'string'},
          {key:'server_hostname', label:'SNI', type:'string'},
          {key:'tls_pin_fingerprint', label:'Pin', type:'string'},
          {key:'tls_tofu', label:'TOFU', type:'boolean'},
          {key:'bind_host', label:'Bind host', type:'string'},
          {key:'bind_port', label:'Bind port', type:'integer', min:0, max:65535},
          {key:'source_ip', label:'Source IP', type:'string'},
          {key:'interface', label:'Interface', type:'string'},
          {key:'fwmark', label:'fwmark', type:'integer'},
          {key:'retry_backoff_initial', label:'Retry backoff initial', type:'number', min:0, step:0.1},
          {key:'retry_backoff_max', label:'Retry backoff max', type:'number', min:0, step:0.1},
          {key:'retry_short_session_sec', label:'Retry short session', type:'number', min:0, step:0.1}
        ])));

        // MuxCon public keys (new schema)
        tables['muxcon.public_keys'] = buildTable('muxcon.public_keys', annotateColumnsWithHelp('muxcon.public_keys', [
          {key:'key_id', label:'Key ID', type:'string', required:true},
          {key:'public_key', label:'Public key', type:'string', required:true},
          // Simple flat filters; advanced per-adapter options can be added later
          {key:'advertise_filters', label:'Advertise filters (JSON)', type:'string', placeholder:'{"include":["remote_*"]}'},
          {key:'accept_filters', label:'Accept filters (JSON)', type:'string', placeholder:'{"server_include":["core"]}'}
        ]));

        // Web console plugins (array of strings -> table of {module})
        tables['web_console.plugins'] = buildTable('web_console.plugins', annotateColumnsWithHelp('web_console.plugins', [
          {key:'module', label:'Module', type:'string', required:true}
        ]), {
          onBeforeRemove: (row)=>{
            if(row && String(row.module||'')===CONFIG_EDITOR_MODULE){
              return confirm('Removing the Config Editor plugin will make the Config page unavailable after save. Continue?');
            }
            return true;
          },
          onAfterChange: ()=>{
            updateAdvisory();
          }
        });
        // Port Actions: action_ports (dict of action_id -> [port_name, ...]) as a table of rows
        tables['port_actions.action_ports'] = buildTable('port_actions.action_ports', annotateColumnsWithHelp('port_actions.action_ports', [
          {key:'action_id', label:'Action ID', type:'string', required:true, placeholder:'echo_probe'},
          {key:'ports', label:'Ports (comma, or *)', type:'array-string'}
        ]));

        // Hook UI toggle warning
        const uiToggle = q('web_console.enable_ui');
        if(uiToggle){
          uiToggle.addEventListener('change', (e)=>{
            if(!uiToggle.checked){
              const ok = confirm('Disabling the web console also removes access to the Config Editor UI. Continue?');
              if(!ok){ uiToggle.checked = true; }
            }
            updateAdvisory();
          });
        }
      }

      function deepGet(obj, path){ const parts=path.split('.'); let cur=obj; for(const p of parts){ if(cur==null) return undefined; if(p in cur) cur=cur[p]; else return undefined; } return cur; }
      function deepSet(obj, path, value){ const parts=path.split('.'); let cur=obj; for(let i=0;i<parts.length-1;i++){ const p=parts[i]; if(!(p in cur) || typeof cur[p] !== 'object' || cur[p]==null){ cur[p]={}; } cur=cur[p]; } cur[parts[parts.length-1]] = value; }

      function populate(data){ current = data || {}; // simple fields
        setVal('server.id', deepGet(current, 'server.id'));
        
        setVal('server.description', deepGet(current, 'server.description'));
        setVal('server.control_socket', deepGet(current, 'server.control_socket'));
        setVal('server.pidfile', deepGet(current, 'server.pidfile'));
        // access_default comes from the /data response, never from the payload: the UI shows it read-only
        setVal('security.access_default', data && data.access_default);

        tables['auth.users']._set(deepGet(current, 'authentication.users')||[]);
        tables['auth.api_keys']._set(deepGet(current, 'authentication.api_keys')||[]);
        tables['auth.public_keys']._set(deepGet(current, 'authentication.public_keys')||[]);
        // External auth populate
        setVal('auth.extauth.enabled', deepGet(current, 'authentication.external_auth.enabled'));
        setVal('auth.extauth.service', deepGet(current, 'authentication.external_auth.service'));
        try {
          const helper = deepGet(current, 'authentication.external_auth.helper');
          if(typeof helper === 'string') setVal('auth.extauth.helper', helper);
          else if(Array.isArray(helper)) setVal('auth.extauth.helper', helper.join(' '));
        }catch(_e){}
        setVal('auth.extauth.timeout', deepGet(current, 'authentication.external_auth.timeout'));
        setVal('auth.extauth.allow_root', deepGet(current, 'authentication.external_auth.allow_root'));
        try { const au = deepGet(current, 'authentication.external_auth.allowed_users'); if(Array.isArray(au)) setVal('auth.extauth.allowed_users', au.join(',')); }catch(_e){}
        setVal('auth.extauth.groups.admin_group', deepGet(current, 'authentication.external_auth.groups.admin_group'));
        setVal('auth.extauth.groups.write_group', deepGet(current, 'authentication.external_auth.groups.write_group'));
        setVal('auth.extauth.groups.read_group', deepGet(current, 'authentication.external_auth.groups.read_group'));
        setVal('auth.extauth.default_permission', deepGet(current, 'authentication.external_auth.default_permission'));

        setVal('logging.level', deepGet(current, 'logging.level'));
        setVal('logging.console', deepGet(current, 'logging.console'));
        setVal('logging.file', deepGet(current, 'logging.file'));
        setVal('logging.log_dir', deepGet(current, 'logging.log_dir'));
        setVal('logging.max_log_size', deepGet(current, 'logging.max_log_size'));
        setVal('logging.log_backup_count', deepGet(current, 'logging.log_backup_count'));

        // client_listener enable from flag if present; else infer from presence
        try { const cl = deepGet(current, 'client_listener'); const en = (cl && typeof cl==='object' && 'enabled' in cl)? !!cl.enabled : !!cl; setVal('client_listener.enabled', en); } catch(_e){}
        setVal('client_listener.host', deepGet(current, 'client_listener.host'));
        setVal('client_listener.port', deepGet(current, 'client_listener.port'));
        setVal('client_listener.max_connections', deepGet(current, 'client_listener.max_connections'));
        setVal('client_listener.connection_timeout', deepGet(current, 'client_listener.connection_timeout'));
        try { const fn = (typeof updateClientListenerUI==='function')? updateClientListenerUI : null; if(fn) fn(); } catch(_e){}

        tables['serial_ports']._set(deepGet(current, 'serial_ports')||[]);
        tables['loopback_ports']._set(deepGet(current, 'loopback_ports')||[]);
        tables['command_ports']._set(deepGet(current, 'command_ports')||[]);
        const tcpInitPorts = deepGet(current, 'tcp_initiator_ports') || deepGet(current, 'openmux_client_ports') || [];
        tables['tcp_initiator_ports']._set((Array.isArray(tcpInitPorts) ? tcpInitPorts : []).map(function(item){
          const flat = Object.assign({}, item);
          const prot = flat.protocol; delete flat.protocol;
          if (prot && typeof prot === 'object') {
            flat.protocol_type = prot.type || 'plain';
            flat.protocol_telnet_negotiation = prot.telnet_negotiation || '';
            flat.protocol_console_name = prot.console_name || '';
            flat.protocol_username = prot.username || '';
            flat.protocol_password = prot.password || '';
            flat.protocol_remote_port = prot.remote_port || '';
            flat.protocol_api_key = prot.api_key || '';
          } else if (flat.remote_port) {
            flat.protocol_type = 'openmux';
            flat.protocol_remote_port = flat.remote_port || '';
            flat.protocol_api_key = flat.api_key || '';
            flat.protocol_username = flat.username || '';
            flat.protocol_password = flat.password || '';
            delete flat.remote_port; delete flat.api_key;
          }
          return flat;
        }));
        tables['telnet_listener']._set(deepGet(current, 'telnet_listener')||[]);
        tables['ssh_listener']._set(deepGet(current, 'ssh_listener')||[]);

        setVal('muxcon.heartbeat_interval', deepGet(current, 'muxcon.heartbeat_interval'));
        setVal('muxcon.mpath_primary_stale_sec', deepGet(current, 'muxcon.mpath_primary_stale_sec'));
        setVal('muxcon.mpath_failover_check_sec', deepGet(current, 'muxcon.mpath_failover_check_sec'));
        setVal('muxcon.mpath_strategy', deepGet(current, 'muxcon.mpath_strategy'));
        setVal('muxcon.mpath_preemptive_promote', deepGet(current, 'muxcon.mpath_preemptive_promote'));
        setVal('muxcon.mpath_neighbor_idle_drop_sec', deepGet(current, 'muxcon.mpath_neighbor_idle_drop_sec'));
        setVal('muxcon.federated_cache_enabled', deepGet(current, 'muxcon.federated_cache_enabled'));
        setVal('muxcon.federated_cache_ttl_sec', deepGet(current, 'muxcon.federated_cache_ttl_sec'));
        setVal('muxcon.federated_cache_path', deepGet(current, 'muxcon.federated_cache_path'));
        setVal('muxcon.auth_required', deepGet(current, 'muxcon.auth_required'));
        setVal('muxcon.auth_key_id', deepGet(current, 'muxcon.auth_key_id'));
        setVal('muxcon.auth_private_key', deepGet(current, 'muxcon.auth_private_key'));
        tables['muxcon.listeners']._set(deepGet(current, 'muxcon.listeners')||[]);
        const inits = deepGet(current, 'muxcon.initiators')||[];
        tables['muxcon.initiators']._set(Array.isArray(inits)? inits: []);
        // MuxCon public keys
        try {
          const mpx = deepGet(current, 'muxcon.public_keys') || [];
          tables['muxcon.public_keys']._set(Array.isArray(mpx)? mpx: []);
        } catch(_e){}

        setVal('web_status.host', deepGet(current, 'web_status.host'));
        setVal('web_status.port', deepGet(current, 'web_status.port'));
        setVal('web_status.enable_http_api', deepGet(current, 'web_status.enable_http_api'));
        setVal('web_status.cors_enable', deepGet(current, 'web_status.cors_enable'));
        setVal('web_status.enable_fault_injection', deepGet(current, 'web_status.enable_fault_injection'));

        setVal('web_console.host', deepGet(current, 'web_console.host'));
        setVal('web_console.port', deepGet(current, 'web_console.port'));
        setVal('web_console.ssl_port', deepGet(current, 'web_console.ssl_port'));
        setVal('web_console.base_path', deepGet(current, 'web_console.base_path'));
        setVal('web_console.respect_forwarded_prefix', deepGet(current, 'web_console.respect_forwarded_prefix'));
        setVal('web_console.enable_ui', deepGet(current, 'web_console.enable_ui'));
        setVal('web_console.realm', deepGet(current, 'web_console.realm'));
        setVal('web_console.motd', deepGet(current, 'web_console.motd'));
        setVal('web_console.logged_in_motd', deepGet(current, 'web_console.logged_in_motd'));
        setVal('web_console.static_dir', deepGet(current, 'web_console.static_dir'));
        setVal('web_console.template_dir', deepGet(current, 'web_console.template_dir'));
        setVal('web_console.enable_probes', deepGet(current, 'web_console.enable_probes'));
        setVal('web_console.probes_include_details', deepGet(current, 'web_console.probes_include_details'));
        setVal('web_console.use_tls', deepGet(current, 'web_console.use_tls'));
        setVal('web_console.ssl_cert', deepGet(current, 'web_console.ssl_cert'));
        setVal('web_console.ssl_key', deepGet(current, 'web_console.ssl_key'));
        setVal('web_console.tls_autogen', deepGet(current, 'web_console.tls_autogen'));
        setVal('web_console.tls_dir', deepGet(current, 'web_console.tls_dir'));
        setVal('web_console.session_ttl_seconds', deepGet(current, 'web_console.session_ttl_seconds'));

        const actionsDirPop = deepGet(current, 'port_actions.actions_dir');
        setVal('port_actions.actions_dir', Array.isArray(actionsDirPop)? actionsDirPop.join('\n') : actionsDirPop);
        try {
          const actionPorts = deepGet(current, 'port_actions.action_ports') || {};
          const rows = Object.keys(actionPorts).map(function(actionId){
            const ports = actionPorts[actionId];
            return {action_id: actionId, ports: Array.isArray(ports)? ports: []};
          });
          if (tables['port_actions.action_ports']) tables['port_actions.action_ports']._set(rows);
        } catch(_e){}

        // web_console.plugins: map ["pkg.mod", ...] -> [{module: "pkg.mod"}, ...]
        const wcPlugins = deepGet(current, 'web_console.plugins') || [];
        if (tables['web_console.plugins']) {
          tables['web_console.plugins']._set((Array.isArray(wcPlugins)? wcPlugins: []).map(m=>({module: m})));
        }
        try{ // Update advisory after populate
          const fn = (typeof updateAdvisory==='function')? updateAdvisory : null; if(fn) fn();
        }catch(_e){}
        try{ injectDefaultHints(); }catch(_e){}
        // Ensure TLS cert/key default hints reflect current tls_dir after populate
        try{ injectDefaultHints(); }catch(_e){}
        
      }

      function buildConfig(){ const out = {}; function maybeSet(path, id){ const v=getVal(id); if(v!==undefined) deepSet(out, path, v); }
        // server
        maybeSet('server.id', 'server.id'); maybeSet('server.description', 'server.description'); maybeSet('server.control_socket', 'server.control_socket'); maybeSet('server.pidfile', 'server.pidfile');
        // auth
        const users = tables['auth.users']._get(); const api_keys = tables['auth.api_keys']._get(); const pub_keys = tables['auth.public_keys']._get(); if(users && users.length>0) deepSet(out, 'authentication.users', users); if(api_keys && api_keys.length>0) deepSet(out, 'authentication.api_keys', api_keys); if(pub_keys && pub_keys.length>0) deepSet(out, 'authentication.public_keys', pub_keys);
        // External auth save: always write the block with the enabled flag
        const eaEnabled = getVal('auth.extauth.enabled');
        const extauth = {};
        if(eaEnabled!==undefined){ extauth.enabled = eaEnabled===true; }
        const eaSvc = getVal('auth.extauth.service'); if(eaSvc!==undefined) extauth.service = eaSvc;
        const eaHelper = getVal('auth.extauth.helper'); if(eaHelper!==undefined) extauth.helper = eaHelper;
        const eaTimeout = getVal('auth.extauth.timeout'); if(eaTimeout!==undefined) extauth.timeout = eaTimeout;
        const eaAllowRoot = getVal('auth.extauth.allow_root'); if(eaAllowRoot!==undefined) extauth.allow_root = eaAllowRoot;
        const eaAu = getVal('auth.extauth.allowed_users'); if(typeof eaAu==='string' && eaAu.trim().length>0){ extauth.allowed_users = eaAu.split(',').map(s=>s.trim()).filter(s=>s.length>0); }
        const eaGroups = {};
        const eaAdmin = getVal('auth.extauth.groups.admin_group'); if(eaAdmin!==undefined) eaGroups.admin_group = eaAdmin;
        const eaWrite = getVal('auth.extauth.groups.write_group'); if(eaWrite!==undefined) eaGroups.write_group = eaWrite;
        const eaRead = getVal('auth.extauth.groups.read_group'); if(eaRead!==undefined) eaGroups.read_group = eaRead;
        if(Object.keys(eaGroups).length>0) extauth.groups = eaGroups;
        const eaDefault = getVal('auth.extauth.default_permission'); if(eaDefault!==undefined) extauth.default_permission = eaDefault;
        deepSet(out, 'authentication.external_auth', extauth);
        // logging
        maybeSet('logging.level','logging.level'); maybeSet('logging.console','logging.console'); maybeSet('logging.file','logging.file'); maybeSet('logging.log_dir','logging.log_dir'); maybeSet('logging.max_log_size','logging.max_log_size'); maybeSet('logging.log_backup_count','logging.log_backup_count');
        // client_listener: explicit enabled flag
        const clEnabled = getVal('client_listener.enabled');
        if(clEnabled===true){
          deepSet(out,'client_listener',{enabled:true});
          maybeSet('client_listener.host','client_listener.host');
          maybeSet('client_listener.port','client_listener.port');
          maybeSet('client_listener.max_connections','client_listener.max_connections');
          maybeSet('client_listener.connection_timeout','client_listener.connection_timeout');
        } else if (clEnabled===false) {
          // Explicitly disabled: write the block with enabled:false to satisfy schema and make intent clear
          deepSet(out,'client_listener',{enabled:false});
        } else if (clEnabled===undefined) {
          // Back-compat: include block when host/port provided
          if(getVal('client_listener.host')!==undefined || getVal('client_listener.port')!==undefined){
            deepSet(out,'client_listener',{enabled:true});
            maybeSet('client_listener.host','client_listener.host');
            maybeSet('client_listener.port','client_listener.port');
            maybeSet('client_listener.max_connections','client_listener.max_connections');
            maybeSet('client_listener.connection_timeout','client_listener.connection_timeout');
          }
        }
        // arrays
        const sps = tables['serial_ports']._get(); if(sps && sps.length>0) deepSet(out,'serial_ports', sps);
        const lps = tables['loopback_ports']._get(); if(lps && lps.length>0) deepSet(out,'loopback_ports', lps);
        const cps = tables['command_ports']._get(); if(cps && cps.length>0) deepSet(out,'command_ports', cps);
        const tipsRaw = tables['tcp_initiator_ports']._get();
        const tips = (tipsRaw||[]).map(function(item){
          const rest = {}; Object.keys(item).forEach(function(k){ if(!k.startsWith('protocol_')) rest[k]=item[k]; });
          const ptype = item.protocol_type || 'plain';
          const prot = {type: ptype}; let hasProto = (ptype !== 'plain');
          if (item.protocol_telnet_negotiation && item.protocol_telnet_negotiation !== 'none') { prot.telnet_negotiation = item.protocol_telnet_negotiation; hasProto = true; }
          if (item.protocol_console_name) prot.console_name = item.protocol_console_name;
          if (item.protocol_username) prot.username = item.protocol_username;
          if (item.protocol_password) prot.password = item.protocol_password;
          if (item.protocol_remote_port) prot.remote_port = item.protocol_remote_port;
          if (item.protocol_api_key) prot.api_key = item.protocol_api_key;
          if (hasProto) rest.protocol = prot;
          return rest;
        });
        if(tips && tips.length>0) deepSet(out,'tcp_initiator_ports', tips);
        const tls = tables['telnet_listener']._get(); if(tls && tls.length>0) deepSet(out,'telnet_listener', tls);
        const shl = tables['ssh_listener']._get(); if(shl && shl.length>0) deepSet(out,'ssh_listener', shl);
        // muxcon
        const listeners = tables['muxcon.listeners']._get(); const inits = tables['muxcon.initiators']._get(); const muxcon={}; ['heartbeat_interval','mpath_primary_stale_sec','mpath_failover_check_sec','mpath_strategy','mpath_preemptive_promote','mpath_neighbor_idle_drop_sec','federated_cache_enabled','federated_cache_ttl_sec','federated_cache_path','auth_required','auth_key_id','auth_private_key'].forEach(k=>{ const v=getVal('muxcon.'+k); if(v!==undefined) muxcon[k]=v; }); if(listeners && listeners.length>0) muxcon.listeners = listeners; if(inits && inits.length>0) muxcon.initiators = inits; const pkRows = tables['muxcon.public_keys']._get(); if(pkRows && pkRows.length>0) muxcon.public_keys = pkRows.map(r=>{ const m={key_id:r.key_id, public_key:r.public_key}; try{ if(r.advertise_filters){ m.advertise_filters = JSON.parse(r.advertise_filters); } }catch(_e){} try{ if(r.accept_filters){ m.accept_filters = JSON.parse(r.accept_filters); } }catch(_e){} return m; }); if(Object.keys(muxcon).length>0) deepSet(out,'muxcon', muxcon);
        // web_status
        if(getVal('web_status.host')!==undefined || getVal('web_status.port')!==undefined){ deepSet(out,'web_status',{}); maybeSet('web_status.host','web_status.host'); maybeSet('web_status.port','web_status.port'); maybeSet('web_status.enable_http_api','web_status.enable_http_api'); maybeSet('web_status.cors_enable','web_status.cors_enable'); maybeSet('web_status.enable_fault_injection','web_status.enable_fault_injection'); }
        // web_console
        if(getVal('web_console.host')!==undefined || getVal('web_console.port')!==undefined){ deepSet(out,'web_console',{}); ['host','port','ssl_port','base_path','respect_forwarded_prefix','enable_ui','realm','motd','logged_in_motd','static_dir','template_dir','enable_probes','probes_include_details','use_tls','ssl_cert','ssl_key','tls_autogen','tls_dir','session_ttl_seconds'].forEach(k=>{ maybeSet('web_console.'+k,'web_console.'+k); }); }
        // web_console.plugins from table -> array of strings
        const wcpl = tables['web_console.plugins'] && tables['web_console.plugins']._get ? tables['web_console.plugins']._get() : [];
        if (wcpl && wcpl.length>0) {
          const arr = wcpl.map(r=>r && r.module).filter(m=>m && String(m).trim().length>0);
          if (arr.length>0) deepSet(out, 'web_console.plugins', arr);
        }
        // port_actions: actions_dir (one path per line; a single line stays a plain string) + action_ports (table rows -> dict of action_id -> ports)
        const portActions = {};
        const actionsDir = getVal('port_actions.actions_dir');
        if(actionsDir!==undefined){
          const actionsDirLines = String(actionsDir).split('\n').map(s=>s.trim()).filter(s=>s.length>0);
          if(actionsDirLines.length>0) portActions.actions_dir = actionsDirLines.length>1? actionsDirLines : actionsDirLines[0];
        }
        const apRows = tables['port_actions.action_ports'] && tables['port_actions.action_ports']._get ? tables['port_actions.action_ports']._get() : [];
        if (apRows && apRows.length>0) {
          const actionPorts = {};
          apRows.forEach(r=>{ if(r && r.action_id){ actionPorts[r.action_id] = Array.isArray(r.ports)? r.ports: []; } });
          if (Object.keys(actionPorts).length>0) portActions.action_ports = actionPorts;
        }
        if (Object.keys(portActions).length>0) deepSet(out, 'port_actions', portActions);
        // management removed
        return out;
      }

async function loadCurrent(){ try{ isPopulating=true; const r=await fetch(withBase('/config-editor/data')); if(!r.ok){ setStatus(false,'Failed to load current config'); return; } const j=await r.json(); populate(j.config||{}); if('writable_sections' in j || 'writable_enforced' in j){ setWritableMetadata(j.writable_sections||[], j.writable_enforced); } if('access_default' in j){ setAccessDefaultReadonly(j.access_default); } markClean(); setStatus(true,'Loaded current config'); }catch(e){ setStatus(false,String(e)); } finally { isPopulating=false; } }
async function validateOnly(){ let payload; try{ payload=buildConfig(); }catch(e){ setStatus(false, e.message||'Validation failed'); return; } try{ const r=await fetch(withBase('/config-editor/validate'),{method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)}); const j=await r.json(); if(r.ok && j.ok){ setStatus(true,'Validation OK'); } else { setStatus(false, (j&&(j.message||j.error)) || 'Validation failed'); } }catch(e){ setStatus(false,String(e)); } }
async function refreshSidebarPorts() {
  try {
    // Small delay to let the server finish port reconciliation before querying
    await new Promise(resolve => setTimeout(resolve, 300));
    const r = await fetch(withBase('/api/ports'), {credentials: 'same-origin'});
    if (!r.ok) return;
    const j = await r.json();
    const ports = j.ports || [];
    const container = document.getElementById('console-ports');
    if (!container) return;
    // Replace existing port links with the updated list
    container.querySelectorAll('a.nav-sub-item, .nav-sub-empty').forEach(el => el.remove());
    if (ports.length > 0) {
      ports.forEach(p => {
        const a = document.createElement('a');
        a.className = 'nav-sub-item';
        a.href = `${BASE_PATH}/console?port=${encodeURIComponent(p.name)}`;
        a.textContent = p.name;
        container.appendChild(a);
      });
    } else {
      const empty = document.createElement('div');
      empty.className = 'nav-sub-empty';
      empty.textContent = 'No ports';
      container.appendChild(empty);
    }
  } catch (_e) {}
}

async function saveConfig(){
  let payload;
  try{ payload=buildConfig(); }
  catch(e){ setStatus(false, e.message||'Save failed'); return; }
  try{
    const headers={'Content-Type':'application/json'}; if(csrf) headers['X-OMX-CSRF']=csrf;
    const r=await fetch(withBase('/config-editor/apply'),{method:'POST', headers, body: JSON.stringify(payload)});
    const j=await r.json();
    if(r.ok && j.ok){
      markClean();
      setStatus(true,'Saved; running soft reload…');
      // Refresh the sidebar immediately after a successful save — the config is on disk
      // regardless of whether the soft reload completes successfully.
      refreshSidebarPorts();
      const reloadOutcome = await requestReload('soft');
      setReloadStatus(reloadOutcome.ok, reloadOutcome.payload);
      if(reloadOutcome.ok){
        const detail = typeof reloadOutcome.payload === 'string' ? reloadOutcome.payload : 'Soft reload completed';
        setStatus(true, `Saved; ${detail}`);
      } else {
        const errMsg = typeof reloadOutcome.payload === 'string' ? reloadOutcome.payload : 'Soft reload failed';
        setStatus(false, `Saved but soft reload failed: ${errMsg}. See Reload tab for details.`);
      }
    } else {
      setStatus(false, (j&&(j.message||j.error)) || 'Save failed');
    }
  }catch(e){ setStatus(false,String(e)); }
}

      BTN('loadBtn').addEventListener('click', loadCurrent);
      BTN('validateBtn').addEventListener('click', validateOnly);
      BTN('saveBtn').addEventListener('click', saveConfig);

// Reload helpers
const reloadStatus = document.getElementById('reloadStatus');
function setReloadStatus(ok, msgOrObj){
  if(!reloadStatus) return;
  reloadStatus.innerHTML='';
  const el=document.createElement('div');
  el.className=ok?'ok':'err';
  if(typeof msgOrObj==='string'){
    el.textContent=msgOrObj;
  } else {
    // Pretty print a small summary
    try{
      const pre=document.createElement('pre');
      pre.style.whiteSpace='pre-wrap';
      pre.textContent = JSON.stringify(msgOrObj, null, 2);
      el.appendChild(pre);
    }catch(_e){ el.textContent = String(msgOrObj); }
  }
  reloadStatus.appendChild(el);
}
async function requestReload(kind){
  try{
    const headers={'Content-Type':'application/json'}; if(csrf) headers['X-OMX-CSRF']=csrf;
    const url = kind==='soft' ? '/config-editor/reload/soft' : '/config-editor/reload/full';
    const r = await fetch(withBase(url), { method:'POST', headers, credentials: 'same-origin' });
    const ct = (r.headers && r.headers.get('content-type')) || '';
    if (r.redirected || (r.url && r.url.indexOf('/login') !== -1) || (ct && ct.indexOf('application/json') === -1)){
      return { ok:false, payload:'Not authorized or session expired. Please sign in again and retry.' };
    }
    const j = await r.json().catch(()=>({}));
    if(r.ok && (j.ok===undefined || j.ok===true)){
      const payload = j.summary || j.message || (kind==='soft' ? 'Soft reload completed' : 'Full reload completed');
      return { ok:true, payload };
    }
    const err = (j&&(j.message||j.error)) || `Reload ${kind} failed`;
    return { ok:false, payload: err };
  }catch(e){
    return { ok:false, payload: String(e) };
  }
}
async function doReload(kind){
  const result = await requestReload(kind);
  setReloadStatus(result.ok, result.payload);
}
const softBtn = document.getElementById('softReloadBtn');
if(softBtn){ softBtn.addEventListener('click', ()=> doReload('soft')); }
const fullBtn = document.getElementById('fullReloadBtn');
if(fullBtn){ fullBtn.addEventListener('click', ()=>{
  const ok = confirm('Full reload will stop and recreate adapters, briefly interrupting connections. Continue?');
  if(!ok) return; doReload('full');
}); }

function updateView() {
    const params = new URLSearchParams(window.location.search);
    const view = params.get('view') || 'ports';
    
    const vSetup = document.getElementById('view-setup');
    const vAuth = document.getElementById('view-auth');
      const vPorts = document.getElementById('view-ports');
      const vListeners = document.getElementById('view-listeners');
    const vMuxcon = document.getElementById('view-muxcon');
    const vActions = document.getElementById('view-actions');
    const vSetup2 = document.getElementById('view-setup-2');
    const vReload = document.getElementById('view-reload');
    
    const navSetup = document.getElementById('nav-config-setup');
    const navAuth = document.getElementById('nav-config-auth');
    const navPorts = document.getElementById('nav-config-ports');
      const navListeners = document.getElementById('nav-config-listeners');
    const navMuxcon = document.getElementById('nav-config-muxcon');
    const navActions = document.getElementById('nav-config-actions');
    const navReload = document.getElementById('nav-config-reload');
    const navParent = document.getElementById('nav-config-parent');
    const pageTitle = document.querySelector('.page');

    if (view === 'ports') {
        if(vSetup) vSetup.style.display = 'none';
          if(vAuth) vAuth.style.display = 'none';
        if(vSetup2) vSetup2.style.display = 'none';
        if(vMuxcon) vMuxcon.style.display = 'none';
        if(vActions) vActions.style.display = 'none';
        if(vReload) vReload.style.display = 'none';
        if(vPorts) vPorts.style.display = 'block';
        if(vListeners) vListeners.style.display = 'none';
        
        if(navSetup) navSetup.classList.remove('active');
          if(navAuth) navAuth.classList.remove('active');
        if(navMuxcon) navMuxcon.classList.remove('active');
        if(navActions) navActions.classList.remove('active');
        if(navReload) navReload.classList.remove('active');
        if(navPorts) navPorts.classList.add('active');
        if(navListeners) navListeners.classList.remove('active');
        if(pageTitle) pageTitle.textContent = 'Config Editor - Ports';
      } else if (view === 'listeners') {
        if(vSetup) vSetup.style.display = 'none';
          if(vAuth) vAuth.style.display = 'none';
        if(vSetup2) vSetup2.style.display = 'none';
        if(vPorts) vPorts.style.display = 'none';
        if(vMuxcon) vMuxcon.style.display = 'none';
        if(vActions) vActions.style.display = 'none';
        if(vReload) vReload.style.display = 'none';
        if(vListeners) vListeners.style.display = 'block';
        
        if(navSetup) navSetup.classList.remove('active');
          if(navAuth) navAuth.classList.remove('active');
        if(navPorts) navPorts.classList.remove('active');
        if(navMuxcon) navMuxcon.classList.remove('active');
        if(navActions) navActions.classList.remove('active');
        if(navReload) navReload.classList.remove('active');
        if(navListeners) navListeners.classList.add('active');
        if(pageTitle) pageTitle.textContent = 'Config Editor - Listeners';
        } else if (view === 'auth') {
          if(vSetup) vSetup.style.display = 'none';
          if(vAuth) vAuth.style.display = 'block';
          if(vSetup2) vSetup2.style.display = 'none';
          if(vPorts) vPorts.style.display = 'none';
          if(vMuxcon) vMuxcon.style.display = 'none';
          if(vActions) vActions.style.display = 'none';
          if(vReload) vReload.style.display = 'none';
          if(vListeners) vListeners.style.display = 'none';
        
          if(navSetup) navSetup.classList.remove('active');
          if(navPorts) navPorts.classList.remove('active');
          if(navListeners) navListeners.classList.remove('active');
          if(navMuxcon) navMuxcon.classList.remove('active');
          if(navActions) navActions.classList.remove('active');
          if(navReload) navReload.classList.remove('active');
          if(navAuth) navAuth.classList.add('active');
          if(pageTitle) pageTitle.textContent = 'Config Editor - Authentication';
    } else if (view === 'muxcon') {
        if(vSetup) vSetup.style.display = 'none';
          if(vAuth) vAuth.style.display = 'none';
        if(vSetup2) vSetup2.style.display = 'none';
        if(vPorts) vPorts.style.display = 'none';
        if(vReload) vReload.style.display = 'none';
        if(vListeners) vListeners.style.display = 'none';
        if(vActions) vActions.style.display = 'none';
        if(vMuxcon) vMuxcon.style.display = 'block';
        
        if(navSetup) navSetup.classList.remove('active');
        if(navPorts) navPorts.classList.remove('active');
        if(navReload) navReload.classList.remove('active');
        if(navListeners) navListeners.classList.remove('active');
          if(navAuth) navAuth.classList.remove('active');
        if(navActions) navActions.classList.remove('active');
        if(navMuxcon) navMuxcon.classList.add('active');
        if(pageTitle) pageTitle.textContent = 'Config Editor - Muxcon';
    } else if (view === 'actions') {
        if(vSetup) vSetup.style.display = 'none';
          if(vAuth) vAuth.style.display = 'none';
        if(vSetup2) vSetup2.style.display = 'none';
        if(vPorts) vPorts.style.display = 'none';
        if(vReload) vReload.style.display = 'none';
        if(vListeners) vListeners.style.display = 'none';
        if(vMuxcon) vMuxcon.style.display = 'none';
        if(vActions) vActions.style.display = 'block';

        if(navSetup) navSetup.classList.remove('active');
        if(navPorts) navPorts.classList.remove('active');
        if(navReload) navReload.classList.remove('active');
        if(navListeners) navListeners.classList.remove('active');
          if(navAuth) navAuth.classList.remove('active');
        if(navMuxcon) navMuxcon.classList.remove('active');
        if(navActions) navActions.classList.add('active');
        if(pageTitle) pageTitle.textContent = 'Config Editor - Actions';
    } else if (view === 'reload') {
        if(vSetup) vSetup.style.display = 'none';
          if(vAuth) vAuth.style.display = 'none';
        if(vSetup2) vSetup2.style.display = 'none';
        if(vPorts) vPorts.style.display = 'none';
        if(vMuxcon) vMuxcon.style.display = 'none';
        if(vActions) vActions.style.display = 'none';
        if(vReload) vReload.style.display = 'block';
        if(vListeners) vListeners.style.display = 'none';
        
        if(navSetup) navSetup.classList.remove('active');
        if(navPorts) navPorts.classList.remove('active');
        if(navMuxcon) navMuxcon.classList.remove('active');
        if(navListeners) navListeners.classList.remove('active');
          if(navAuth) navAuth.classList.remove('active');
        if(navActions) navActions.classList.remove('active');
        if(navReload) navReload.classList.add('active');
        if(pageTitle) pageTitle.textContent = 'Config Editor - Reload';
    } else {
        if(vSetup) vSetup.style.display = 'block';
          if(vAuth) vAuth.style.display = 'none';
        if(vSetup2) vSetup2.style.display = 'block';
        if(vPorts) vPorts.style.display = 'none';
        if(vMuxcon) vMuxcon.style.display = 'none';
        if(vActions) vActions.style.display = 'none';
        if(vReload) vReload.style.display = 'none';
        if(vListeners) vListeners.style.display = 'none';
        
        if(navSetup) navSetup.classList.add('active');
        if(navPorts) navPorts.classList.remove('active');
        if(navMuxcon) navMuxcon.classList.remove('active');
        if(navActions) navActions.classList.remove('active');
        if(navReload) navReload.classList.remove('active');
        if(navListeners) navListeners.classList.remove('active');
          if(navAuth) navAuth.classList.remove('active');
        if(pageTitle) pageTitle.textContent = 'Config Editor - Server';
    }
    // Ensure parent is active
    if(navParent) navParent.classList.add('active');
}

const formRoot = document.getElementById('formRoot');
if(formRoot){
  formRoot.addEventListener('input', markDirty);
  formRoot.addEventListener('change', markDirty);
}

fetchCSRF();
initStatic();
injectHelps();
injectReloadHints();
injectDefaultHints();
setWritableMetadata(INITIAL_WRITABLE_SECTIONS, INITIAL_WRITABLE_ENFORCED);
loadCurrent();
updateView();
    })();
