# Fault Injection via Web Status Adapter

Enable in the top-level `web_status:` section of `config/server.yaml`:

```yaml
web_status:
  port: 8080
  enable_http_api: true
  enable_fault_injection: true
```

Restart the server, then list federation connections:

```sh
curl -s http://localhost:8080/api/federation | jq '.connections[] | {id: .connection_id, role: .role}'
```

Use the returned `connection_id` (e.g. `out:127.0.0.1:7822:1713781200`).

## Actions
POST JSON to `/api/fault`:

| action | params | Effect |
|--------|--------|--------|
| list | (none) | Return current fault state map |
| freeze | connection_id | Mark connection frozen (read loop and heartbeat aging suppress it) |
| unfreeze | connection_id | Remove frozen flag |
| drop_heartbeats | connection_id | Suppress heartbeats (simulates dead peer from our side) |
| restore_heartbeats | connection_id | Re-enable heartbeats |
| close_conn | connection_id, linger (sec, optional) | Gracefully close socket after optional delay |
| reset_conn | connection_id | Force RST close using SO_LINGER(0) |

## Examples

Drop heartbeats:
```sh
curl -X POST http://localhost:8080/api/fault \
  -H 'Content-Type: application/json' \
  -d '{"action":"drop_heartbeats","connection_id":"out:127.0.0.1:7822:1713781200"}' | jq
```

Restore:
```sh
curl -X POST http://localhost:8080/api/fault \
  -H 'Content-Type: application/json' \
  -d '{"action":"restore_heartbeats","connection_id":"out:127.0.0.1:7822:1713781200"}' | jq
```

Graceful close after 2s:
```sh
curl -X POST http://localhost:8080/api/fault \
  -H 'Content-Type: application/json' \
  -d '{"action":"close_conn","connection_id":"out:127.0.0.1:7822:1713781200","params":{"linger":2}}' | jq
```

Immediate RST:
```sh
curl -X POST http://localhost:8080/api/fault \
  -H 'Content-Type: application/json' \
  -d '{"action":"reset_conn","connection_id":"out:127.0.0.1:7822:1713781200"}' | jq
```

List current fault flags:
```sh
curl -s -X POST http://localhost:8080/api/fault -H 'Content-Type: application/json' -d '{"action":"list"}' | jq
```

---
Note: `freeze` stops the read loop for the connection (frames are no longer
consumed) without closing the socket, and ages the connection's `last_seen`
so the multi-path failover logic treats it as stale and promotes another
path. `unfreeze` restores normal reads.
