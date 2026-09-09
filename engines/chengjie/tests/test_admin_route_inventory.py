"""admin.py 路由清单快照（Phase E1 重构安全网）。

冻结基线：admin app 在重构期间必须保持「所有基线端点仍注册」。
- 任何拆分/搬迁导致端点丢失 → 本测试失败。
- 新增端点不会破坏（只断言基线是实际清单的子集）。

基线由 2026-06-01 admin.py（6819 行，拆分前）抓取，total=457。
若刻意删除/改名端点，请同步更新本基线并在 PR 说明。
"""

from starlette.routing import Route

# 基线端点（path<TAB>methods，methods 为逗号分隔且不含 HEAD/OPTIONS）。
_BASELINE = """
/	GET
/manifest.webmanifest	GET
/sw.js	GET
/admin/ops	GET
/admin/tts-dashboard	GET
/admin/voice-eval	GET
/api/admin/voice-eval/state	GET
/api/admin/voice-eval/rate	POST
/api/admin/voice-eval/audio/{name}	GET
/ai-studio	GET
/analytics	GET
/api/ab-tests/evaluate	GET
/api/ab-tests/{intent}	PUT
/api/activity-stats	GET
/api/admin/branding	GET
/api/admin/branding	POST
/api/admin/demo	GET
/api/admin/demo/clear	POST
/api/admin/demo/seed	POST
/api/admin/health	GET
/api/admin/incidents	GET
/api/admin/incidents/{incident_id}/ack	POST
/api/admin/health/recheck	POST
/api/admin/ops-overview	GET
/api/admin/ops-report	GET
/api/admin/tts-cost-trend	GET
/api/admin/translation-confidence-trend	GET
/api/admin/frontend-error-trend	GET
/api/admin/login-funnel-trend	GET
/api/admin/ui-event-trend	GET
/api/admin/identity-health-trend	GET
/api/admin/anti-repeat-advice	GET
/api/admin/ai-safety-overview	GET
/api/admin/alert-link-status	GET
/api/admin/official-webhook-status	GET
/api/admin/buried-conversations	GET
/api/admin/bug-intake	GET
/api/admin/bug-intake/notify-pending	POST
/api/admin/bug-intake/{ticket_id}/notify	POST
/api/admin/bug-intake/{ticket_id}/shot/{name}	GET
/api/admin/bug-intake/{ticket_id}/shots	GET
/api/admin/bug-intake/{ticket_id}/status	POST
/api/admin/asset/reconnect/inventory	GET
/api/admin/asset/reconnect/candidates	GET
/api/admin/asset/reconnect/claim	POST
/api/admin/asset/reconnect/auto-claim	POST
/api/admin/asset/reconnect/claims	GET
/api/assistant/bootstrap	GET
/api/assistant/query	POST
/api/assistant/report	POST
/api/assistant/tickets	GET
/api/assistant/feedback	POST
/api/assistant/transcribe	POST
/api/assistant/health	GET
/api/assistant/terms	GET
/api/assistant/actions	GET
/api/assistant/act	POST
/api/assistant/act/history	GET
/api/assistant/act/undo	POST
/api/assistant/agent/plan	POST
/api/assistant/pair	POST
/api/assistant/pair/sessions	GET
/api/assistant/pair/revoke	POST
/api/assistant/flows	GET
/xz	GET
/xz/manifest.webmanifest	GET
/api/admin/ui-event	POST
/api/telemetry/frontend-error	POST
/api/admin/ai-quality-calibrate	GET
/api/admin/ai-quality-thresholds	POST
/api/admin/instance-restart-status	GET
/api/admin/platform-sessions/relogin	POST
/api/admin/platform-sessions/e2ee-pin	POST
/api/admin/profile-audit	GET
/api/admin/realtime-voice-alert-calibrate	GET
/api/admin/realtime-voice-alert-thresholds	POST
/api/admin/realtime-voice-trend	GET
/api/admin/gpu-watermark	GET
/api/admin/automation-coverage	GET
/api/admin/acquisition-health	GET
/api/admin/send-route-trend	GET
/api/admin/media-promise-trend	GET
/api/admin/media-consistency	GET
/api/admin/diagnostic-bundle	GET
/api/admin/duel-bench	GET
/api/admin/workers/{worker_id}/reset-circuit	POST
/api/admin/reliability	GET
/api/admin/license	GET
/api/admin/license/reload	POST
/api/admin/license/activate	POST
/api/admin/tts-cleanup	POST
/api/admin/tts-stats	GET
/api/ai-studio/summary	GET
/api/ai/quality	GET
/api/alert-status	GET
/api/analytics	GET
/api/apply-param-suggestion	POST
/api/audit	GET
/api/audit/activity	GET
/api/autopilot	PUT
/api/autopilot-status	GET
/api/batch-channels	POST
/api/batch-strategies	POST
/api/batch-templates	POST
/api/bot-metrics	GET
/api/care/schedule	GET
/api/care/schedule	POST
/api/care/schedule/due	GET
/api/care/schedule/{sid}/cancel	POST
/api/care/schedule/{sid}/send-now	POST
/api/care/schedule/{sid}/reschedule	POST
/api/care/schedule/{sid}/preview	POST
/api/care/dry-run-samples	GET
/api/care/dry-run-feedback	POST
/api/care/health	GET
/api/care/plan	GET
/api/care/engine	POST
/api/deferred-outbox/status	GET
/api/deferred-outbox/retry	POST
/api/deferred-outbox/cancel	POST
/api/deferred-outbox/pause	POST
/api/deferred-outbox/resume	POST
/api/monetize/overview	GET
/api/monetize/catalog	GET
/api/monetize/entitlement	GET
/api/monetize/retention	GET
/api/monetize/teaser-funnel	GET
/api/monetize/selfie-funnel	GET
/api/monetize/selfie-cap	GET
/api/monetize/selfie-contacts	GET
/api/monetize/teaser-contacts	GET
/api/monetize/feature-check	POST
/api/monetize/grant	POST
/api/monetize/webhook	POST
/api/monetize/checkout	POST
/api/monetize/webhook/stripe	POST
/api/monetize/webhook/telegram	POST
/api/cases/active	GET
/api/cases/close-drill	POST
/api/cases/{case_id}/close	POST
/api/cases/{case_id}/note	POST
/api/change-password	POST
/api/channels	GET
/api/channels/{channel}	PUT
/api/chat/test	POST
/api/chat/test/correct	POST
/api/config/summary	GET
/api/conversations/active	GET
/api/copilot/query	POST
/api/crisis-events	GET
/api/crisis-events/enable	POST
/api/crisis-events/{event_id}/handle	POST
/api/data-purge	POST
/api/episodic-memory	GET
/api/episodic-memory/backfill	POST
/api/episodic-memory/bulk-delete	POST
/api/episodic-memory/correction-stats	GET
/api/episodic-memory/export	GET
/api/episodic-memory/summary	GET
/api/episodic-memory/{row_id}	DELETE
/api/episodic-memory/{row_id}	PUT
/api/episodic-memory/{row_id}/confirm	POST
/api/episodic-memory/{row_id}/ignore	POST
/api/episodic-memory/{row_id}/restore	POST
/api/episodic-memory/resolve-conflict	POST
/api/episodic-memory/promises/open	GET
/api/episodic-memory/review-queue	GET
/api/episodic-memory/self-log	GET
/api/episodic-memory/self-log/delete	POST
/api/episodic-memory/self-log/mark-done	POST
/api/episodic-memory/used	GET
/api/unified-inbox/send-status	GET
/api/episodic-memory/key-health	GET
/api/episodic-memory/key-migrate	POST
/api/episodic-memory/key-migrate/plan	GET
/api/events	GET
/api/export-strategy-events	GET
/api/health-check	GET
/api/human-escalation/mention-round-robin	GET
/api/human-escalation/schedule-status	GET
/api/human-escalation/shift	GET
/api/human-escalation/shift	POST
/api/human-escalation/verify	GET
/api/i18n/bundle	GET
/api/identity	GET
/api/identity/link	POST
/api/identity/shadow/confirm-link	POST
/api/identity/shadow/dismiss	POST
/api/identity/shadow/evidence	GET
/api/identity/unlink	POST
/api/kb/accept-suggestion	POST
/api/kb/ai-generate	POST
/api/kb/auto-suggestions	GET
/api/kb/backup	POST
/api/kb/backups	GET
/api/kb/category-stats	GET
/api/kb/check-channel-conflict	POST
/api/kb/check-conflict	POST
/api/kb/check-trigger-overlaps	POST
/api/kb/duplicates	GET
/api/kb/embed-all	POST
/api/kb/embed-coverage	GET
/api/kb/embed-progress	GET
/api/kb/embed-stats	GET
/api/kb/entries	GET
/api/kb/entries	POST
/api/kb/entries/batch-update	POST
/api/kb/entries/bulk-disable	POST
/api/kb/entries/purge-source	POST
/api/kb/entries/{entry_id}	DELETE
/api/kb/entries/{entry_id}	GET
/api/kb/entries/{entry_id}	PUT
/api/kb/entries/{entry_id}/auto-translate	POST
/api/kb/entries/{entry_id}/embed	POST
/api/kb/entries/{entry_id}/images	GET
/api/kb/entries/{entry_id}/images	POST
/api/kb/entries/{entry_id}/translate	POST
/api/kb/entries/{entry_id}/versions	GET
/api/kb/error-codes	GET
/api/kb/error-codes	POST
/api/kb/error-codes/{ec_id}	DELETE
/api/kb/error-codes/{ec_id}	PUT
/api/kb/evolve-sweep	POST
/api/kb/examples	GET
/api/kb/examples	POST
/api/kb/examples/{ex_id}	DELETE
/api/kb/export	GET
/api/kb/export-csv	GET
/api/kb/export-markdown	GET
/api/kb/feedback	GET
/api/kb/feedback	POST
/api/kb/feedback/{fb_id}/promote	POST
/api/kb/health	GET
/api/kb/health-stats	GET
/api/kb/images/{img_id}	DELETE
/api/kb/implicit-feedback	POST
/api/kb/import	POST
/api/kb/import-csv	POST
/api/kb/import/save	POST
/api/kb/maintenance-advice	GET
/api/kb/miss-log	DELETE
/api/kb/miss-log	POST
/api/kb/miss-to-entry	POST
/api/kb/query-analytics	GET
/api/kb/reply-quality	GET
/api/kb/report	GET
/api/kb/restore/{filename}	POST
/api/kb/rules	GET
/api/kb/rules	POST
/api/kb/rules/{rule_id}	DELETE
/api/kb/sandbox	POST
/api/kb/sandbox/ai-reply	POST
/api/kb/sandbox/save-example	POST
/api/kb/cold-start	GET
/api/kb/seed-pack	POST
/api/kb/improvements	GET
/api/kb/improvements/convert	POST
/api/kb/seed	POST
/api/kb/self-heal	POST
/api/kb/stale	GET
/api/kb/stats	GET
/api/kb/today-hit-rate	GET
/api/kb/translate-all	POST
/api/kb/translate-progress	GET
/api/kb/translate-sweep	POST
/api/kb/translation-gaps	GET
/api/kb/translations/pending	GET
/api/kb/translations/{trans_id}	PUT
/api/kb/translations/{trans_id}/confirm	POST
/api/kb/translations/{trans_id}/retranslate	POST
/api/kb/usage-ranking	GET
/api/kb/versions/{version_id}	GET
/api/kb/versions/{version_id}/restore	POST
/api/learner/drafts	GET
/api/learner/drafts/approve-all	POST
/api/learner/drafts/batch-action	POST
/api/learner/drafts/{draft_id}	GET
/api/learner/drafts/{draft_id}	PUT
/api/learner/drafts/{draft_id}/approve	POST
/api/learner/drafts/{draft_id}/recheck-dup	POST
/api/learner/drafts/{draft_id}/reject	POST
/api/learner/drafts/translate	POST
/api/learner/feed	POST
/api/learner/run	POST
/api/learner/stats	GET
/api/line-rpa/accept-friends	POST
/api/line-rpa/alerts	GET
/api/line-rpa/alerts/ack_all	POST
/api/line-rpa/alerts/{alert_id}/ack	POST
/api/line-rpa/audit	GET
/api/line-rpa/chat-history/{chat_key:path}	GET
/api/line-rpa/chat-lang-lock	POST
/api/line-rpa/chats	GET
/api/line-rpa/config	GET
/api/line-rpa/config	PUT
/api/line-rpa/customer-profile/{chat_key:path}	GET
/api/line-rpa/device-screenshot	GET
/api/line-rpa/intent-stats	GET
/api/line-rpa/log-tail	GET
/api/line-rpa/metrics	GET
/api/line-rpa/notifications	GET
/api/line-rpa/pause	POST
/api/line-rpa/pending	GET
/api/line-rpa/pending-tts	GET
/api/line-rpa/pending/cancel-all	POST
/api/line-rpa/pending/{pending_id}/resolve	POST
/api/line-rpa/pending/{pending_id}/retry-tts	POST
/api/line-rpa/recent	GET
/api/line-rpa/resume	POST
/api/line-rpa/screenshot/{name}	GET
/api/line-rpa/search	GET
/api/line-rpa/send-manual	POST
/api/line-rpa/send-queue	GET
/api/line-rpa/send-queue/{item_id}	GET
/api/line-rpa/send-queue/{item_id}/cancel	POST
/api/line-rpa/sessions/{chat_key:path}	GET
/api/line-rpa/status	GET
/api/line-rpa/timeline	GET
/api/line-rpa/trigger	POST
/api/messenger-rpa/accounts	GET
/api/messenger-rpa/accounts/health	GET
/api/messenger-rpa/accounts/{account_id}/chats/emergency_stop	DELETE
/api/messenger-rpa/accounts/{account_id}/chats/emergency_stop	POST
/api/messenger-rpa/accounts/{account_id}/chats/skipped	GET
/api/messenger-rpa/accounts/{account_id}/clear-unsafe	POST
/api/messenger-rpa/accounts/{account_id}/pause	POST
/api/messenger-rpa/accounts/{account_id}/resume	POST
/api/messenger-rpa/accounts/{account_id}/send-to	POST
/api/messenger-rpa/accounts/{account_id}/trigger	POST
/api/messenger-rpa/approvals	GET
/api/messenger-rpa/approvals/batch	POST
/api/messenger-rpa/approvals/{approval_id}	GET
/api/messenger-rpa/approvals/{approval_id}/approve	POST
/api/messenger-rpa/approvals/{approval_id}/reject	POST
/api/messenger-rpa/approvals/{approval_id}/suggest	POST
/api/messenger-rpa/approvals/{approval_id}/update	POST
/api/messenger-rpa/bindings	GET
/api/messenger-rpa/bindings	PUT
/api/messenger-rpa/calibrate	POST
/api/messenger-rpa/chat-history/{chat_key:path}	GET
/api/messenger-rpa/chat-lang-lock	POST
/api/messenger-rpa/chat-persona-bindings	GET
/api/messenger-rpa/chat-persona-bindings/batch	POST
/api/messenger-rpa/chat-persona-bindings/{chat_name}	DELETE
/api/messenger-rpa/chat-persona-bindings/{chat_name}	PUT
/api/messenger-rpa/chat/history	GET
/api/messenger-rpa/config	GET
/api/messenger-rpa/config	PUT
/api/messenger-rpa/coordinator	GET
/api/messenger-rpa/credits	GET
/api/messenger-rpa/credits/{chat_key}/reset	POST
/api/messenger-rpa/customer-profile/{chat_key:path}	GET
/api/messenger-rpa/devices	GET
/api/messenger-rpa/funnel	GET
/api/messenger-rpa/hint-metrics	GET
/api/messenger-rpa/install-adbkeyboard	POST
/api/messenger-rpa/intent-stats	GET
/api/messenger-rpa/leads	GET
/api/messenger-rpa/leads/{chat_key:path}	GET
/api/messenger-rpa/leads/{chat_key:path}/handoff	PUT
/api/messenger-rpa/llm-cost	GET
/api/messenger-rpa/media	GET
/api/messenger-rpa/media	PUT
/api/messenger-rpa/media/asr-test	POST
/api/messenger-rpa/media/tts-test	POST
/api/messenger-rpa/metrics	GET
/api/messenger-rpa/mobile-auto/cluster/devices/{device_id}/screenshot	GET
/api/messenger-rpa/mobile-auto/devices/{device_id}/action	POST
/api/messenger-rpa/mobile-auto/devices/{device_id}/screenshot	GET
/api/messenger-rpa/mobile-auto/status	GET
/api/messenger-rpa/pause	POST
/api/messenger-rpa/personas	GET
/api/messenger-rpa/personas	PUT
/api/messenger-rpa/recent	GET
/api/messenger-rpa/replays	GET
/api/messenger-rpa/replays/rerun	POST
/api/messenger-rpa/resume	POST
/api/messenger-rpa/search	GET
/api/messenger-rpa/send-manual	POST
/api/messenger-rpa/send-queue	GET
/api/messenger-rpa/send-queue/{item_id}	GET
/api/messenger-rpa/send-queue/{item_id}/cancel	POST
/api/messenger-rpa/sessions/{chat_key:path}	GET
/api/messenger-rpa/status	GET
/api/messenger-rpa/strategy/accounts/{account_id}	PATCH
/api/messenger-rpa/strategy/audit/{audit_id}/rollback	POST
/api/messenger-rpa/strategy/conversations/{customer_id:path}	PATCH
/api/messenger-rpa/strategy/jobs/{job_id}/{action}	POST
/api/messenger-rpa/strategy/personas	POST
/api/messenger-rpa/strategy/personas/{persona_id}	PATCH
/api/messenger-rpa/strategy/personas/{persona_id}/{action}	POST
/api/messenger-rpa/strategy/runtime	GET
/api/messenger-rpa/strategy/simulate	POST
/api/messenger-rpa/templates	GET
/api/messenger-rpa/trigger	POST
/api/messenger-rpa/variants/stats	GET
/api/migrate	POST
/api/model-summary	GET
/api/notifications	GET
/api/persona	GET
/api/persona/bind	POST
/api/persona/bindings	GET
/api/persona/global-rules	GET
/api/persona/global-rules	PUT
/api/persona/global-rules/backups	GET
/api/persona/global-rules/preview	POST
/api/persona/global-rules/restore/{slot}	POST
/api/persona/legacy-bindings	GET
/api/persona/legacy-bindings/cleanup	POST
/api/persona/legacy-bindings/cleanup-all	POST
/api/persona/preview-prompt	GET
/api/persona/unbind	POST
/api/persona/update-default	POST
/api/ops/kill-switch	GET
/api/ops/kill-switch	POST
/api/ops/kill-switch	DELETE
/api/ops/canary	GET
/api/ops/canary	POST
/api/ops/canary	DELETE
/api/personas/bulk-bind	POST
/api/personas/import-doc/status	GET
/api/personas/import-doc/parse	POST
/api/personas/import-doc/extract	POST
/api/personas/import-doc/jobs/{job_id}	GET
/api/personas/list	GET
/api/personas/mrpa-account/{account_id}/assign-profile	POST
/api/personas/profiles	GET
/api/personas/profiles/export	GET
/api/personas/profiles/import	POST
/api/personas/profiles/reload	POST
/api/personas/profiles/{profile_id}	DELETE
/api/personas/profiles/{profile_id}	GET
/api/personas/profiles/{profile_id}	PUT
/api/personas/profiles/{profile_id}/bindings	GET
/api/personas/profiles/{profile_id}/content-scan	GET
/api/personas/profiles/{profile_id}/diff-canonical	GET
/api/personas/profiles/{profile_id}/history	GET
/api/personas/profiles/{profile_id}/promote	POST
/api/personas/profiles/{profile_id}/prompt-preview	GET
/api/personas/profiles/{profile_id}/retire-verify	POST
/api/personas/profiles/{profile_id}/revert	POST
/api/personas/registry-account/{platform}/{account_id}/assign-profile	POST
/api/personas/schema-keys	GET
/api/personas/selfie-gate	GET
/api/personas/selfie-gate	POST
/api/personas/status	GET
/api/personas/sync-to-config	POST
/api/personas/tg-account/{account_id}/assign-profile	POST
/api/personas/wa-account/{account_id}/assign-profile	POST
/api/personas/{pid}/media	GET
/api/personas/{pid}/media	POST
/api/personas/{pid}/media/test	POST
/api/personas/{pid}/media/retag-all	POST
/api/personas/{pid}/media/{mid}/retag	POST
/api/personas/{pid}/media/{mid}	DELETE
/api/personas/{pid}/media/{mid}	PATCH
/api/personas/{pid}/speech-print	POST
/api/personas/{pid}/stock-readiness	GET
/api/personas/{pid}/face-ref	GET
/api/personas/{pid}/face-ref	POST
/api/personas/{pid}/face-ref	DELETE
/api/personas/{pid}/face-ref/image	GET
/api/reactivation/dry-run-feedback	POST
/api/reactivation/dry-run-samples	GET
/api/relations/capability	GET
/api/registry/apply-template	POST
/api/registry/batch	POST
/api/registry/export	GET
/api/registry/templates	GET
/api/reply-logic	GET
/api/reply-logic	POST
/api/reply-settings	GET
/api/reply-settings	POST
/api/reply-settings/budget-today	GET
/api/reply-settings/explain	GET
/api/reply-settings/follow-slider	POST
/api/reply-settings/health	GET
/api/report/daily	GET
/api/report/weekly	GET
/api/rollback	POST
/api/rpa-overview/alerts	GET
/api/rpa-overview/control	POST
/api/rpa-overview/device-stats	GET
/api/rpa-overview/device-stats/{serial}	GET
/api/rpa-overview/devices	GET
/api/rpa-overview/events	GET
/api/rpa-overview/lang-dist	GET
/api/rpa-overview/lang-dist-version	GET
/api/rpa-overview/lang-trend	GET
/api/rpa-overview/load-balance	GET
/api/rpa-overview/pending	GET
/api/rpa-overview/registry	GET
/api/rpa-overview/registry	POST
/api/rpa-overview/registry/{serial}/auto-detect	POST
/api/rpa-overview/status	GET
/api/rpa/cross-platform-profile	GET
/api/rpa/global-search	GET
/api/rpa/intent-tags	GET
/api/rpa/intent-tags	POST
/api/rpa/intent-tags/backups	GET
/api/rpa/intent-tags/diff	POST
/api/rpa/intent-tags/raw	GET
/api/rpa/intent-tags/reload	POST
/api/rpa/intent-tags/restore	POST
/api/rpa/metrics	GET
/api/session-stats	GET
/api/sessions	GET
/api/sessions/revoke-all	POST
/api/sessions/{jti}/revoke	POST
/api/settings/intent-keywords	GET
/api/settings/intent-keywords	PUT
/api/settings/save	POST
/api/settings/test-intent	POST
/api/settings/test-webhook	GET
/api/setup	POST
/api/setup/test-ai	POST
/api/snapshots	GET
/api/strategies	GET
/api/strategies/mapping	PUT
/api/strategies/{strategy_id}	PUT
/api/strategy-analytics	GET
/api/strategy-analytics/compare	GET
/api/strategy-analytics/{strategy_id}/hourly	GET
/api/strategy-history/{strategy_id}	GET
/api/system-info	GET
/api/telegram/account-info	GET
/api/telegram/config-export	GET
/api/telegram/config-restore	POST
/api/telegram/config-snapshots	GET
/api/telegram/health	GET
/api/telegram/log-stream	GET
/api/platforms/{platform}/modes	GET
/api/platforms/{platform}/login/start	POST
/api/platforms/{platform}/login/{login_id}/status	GET
/api/platforms/{platform}/login/{login_id}/relay-step	GET
/api/platforms/{platform}/login/{login_id}/relay-submit	POST
/api/platforms/{platform}/login/{login_id}/cancel	POST
/api/proxies	GET
/api/proxies	POST
/api/proxies/managed/status	GET
/api/proxies/managed/provision	POST
/api/proxies/managed/swap	POST
/api/proxies/managed/overview	GET
/api/proxies/import	POST
/api/proxies/{proxy_id}	DELETE
/api/proxies/{proxy_id}/test	POST
/api/fingerprints	GET
/api/fingerprints/generate	POST
/api/accounts	GET
/api/accounts/orchestrator	GET
/api/accounts/fleet-health	GET
/api/accounts/send-health	GET
/api/accounts/orchestrator/sync	POST
/api/accounts/{platform}/{account_id}/start	POST
/api/accounts/{platform}/{account_id}/stop	POST
/api/accounts/{platform}/{account_id}/restart	POST
/api/accounts/{platform}/{account_id}/logout	POST
/api/accounts/{platform}/{account_id}/remove	POST
/api/accounts/{platform}/{account_id}/label	POST
/api/accounts/{platform}/{account_id}/auto-reply	POST
/api/accounts/{platform}/{account_id}/auto-reply/override	POST
/api/accounts/auto-reply/audit	GET
/api/accounts/auto-reply/config	GET
/api/accounts/auto-reply/config	POST
/api/accounts/auto-reply/health	GET
/api/accounts/auto-reply/webhooks	GET
/api/accounts/auto-reply/webhooks	POST
/api/accounts/auto-reply/webhooks/test	POST
/api/accounts/auto-reply/stream	GET
/api/accounts/protocol/readiness	GET
/api/internal/ops/maintenance-notice	POST
/api/internal/protocol/ingest	POST
/api/internal/protocol/session-status	POST
/api/internal/protocol/inbox-health	POST
/api/internal/protocol/thread-history	POST
/api/internal/protocol/contacts	POST
/api/internal/protocol/chats	POST
/api/internal/protocol/reaction	POST
/api/internal/protocol/receipt	POST
/api/internal/protocol/presence	POST
/api/internal/protocol/message-op	POST
/api/platforms/{platform}/{account_id}/avatar	GET
/api/platforms/telegram/{account_id}/resolve-peer	GET
/api/platforms/{platform}/{account_id}/contacts	GET
/api/platforms/{platform}/{account_id}/contacts/refresh	POST
/api/platforms/{platform}/{account_id}/history	POST
/api/platforms/{platform}/{account_id}/sync-groups	POST
/api/platforms/{platform}/{account_id}/subscribe-presence	POST
/api/platforms/messenger/{account_id}/request-action	POST
/api/platforms/{platform}/{account_id}/react	POST
/api/platforms/{platform}/{account_id}/message-op	POST
/api/platforms/{platform}/{account_id}/group-members	GET
/api/unified-inbox/send-media	POST
/api/unified-inbox/media-download	GET
/api/unified-inbox/send-voice	POST
/api/unified-inbox/send-voice-status	GET
/api/unified-inbox/send-gate/exempt	POST
/api/unified-inbox/send-caps	GET
/api/workspace/quota	GET
/api/desktop/ping	GET
/api/group-show/attendance/invite	POST
/api/desktop/smart-reply	POST
/api/desktop/guard-check	POST
/api/desktop/ingest	POST
/api/desktop/selector-profiles	GET
/api/desktop/selector-profiles/path	GET
/api/desktop/selector-profiles/validate	GET
/api/desktop/inject-health	POST
/api/desktop/inject-health	GET
/api/desktop/inject-health/alerts	GET
/api/desktop/inject-health/extract-trend	GET
/api/desktop/outbound	GET
/api/desktop/outbound/ack	POST
/api/desktop/outbound/action	POST
/api/desktop/outbound/corrections	GET
/api/desktop/outbound/rewrite	POST
/api/desktop/outbound/stats	GET
/api/desktop/fingerprint	GET
/api/desktop/ui-flags	GET
/api/developer/ui-visibility	GET
/api/developer/ui-visibility	POST
/api/developer/developer-mode	POST
/api/telegram/log-tail	GET
/api/telegram/recent-contacts	GET
/api/telegram/settings	GET
/api/telegram/settings/reply-logic	PUT
/api/telegram/settings/voice-asr	PUT
/api/telegram/settings/voice-reply	PUT
/api/telegram/upload-voice	POST
/api/telegram/voice-files	GET
/api/telegram/voice-quality	GET
/api/telegram/voice-sample/{filename}	GET
/api/templates	GET
/api/templates/{key}	PUT
/api/templates-i18n	GET
/api/templates-i18n/confirm	POST
/api/templates-i18n/delete	POST
/api/templates-i18n/draft	POST
/api/templates-i18n/save	POST
/api/todo-summary	GET
/api/trigger-decisions	GET
/api/unified-inbox/analyze	POST
/api/unified-inbox/automation	GET
/api/unified-inbox/automation	POST
/api/unified-inbox/automation/bulk-downgrade	POST
/api/unified-inbox/automation-stats	GET
/api/unified-inbox/bot-flag	GET
/api/unified-inbox/bot-flag	POST
/api/unified-inbox/conv-probe	GET
/api/unified-inbox/why-no-reply	GET
/api/unified-inbox/warmup-review	POST
/api/unified-inbox/platform-cap	POST
/api/unified-inbox/reply-budget/relief	POST
/api/unified-inbox/messenger/e2ee-pin	POST
/api/unified-inbox/chats	GET
/api/unified-inbox/history	GET
/api/unified-inbox/kb-search	GET
/api/unified-inbox/mark-conversion	POST
/api/unified-inbox/search-messages	GET
/api/unified-inbox/profile	GET
/api/unified-inbox/templates	GET
/api/unified-inbox/send	POST
/api/unified-inbox/tg-join-chat	POST
/api/unified-inbox/stored-chats	GET
/api/unified-inbox/outreach/batch	GET
/api/unified-inbox/outreach/execute	POST
/api/unified-inbox/outreach/preview	POST
/api/unified-inbox/thread	GET
/api/unified-inbox/conv-engine	GET
/api/unified-inbox/conv-engine	POST
/api/unified-inbox/default-lang	GET
/api/unified-inbox/default-lang	POST
/api/unified-inbox/default-lang/all	GET
/api/unified-inbox/default-reply-lang	GET
/api/unified-inbox/default-reply-lang	POST
/api/unified-inbox/default-reply-lang/all	GET
/api/unified-inbox/agent-lang	GET
/api/unified-inbox/agent-lang	POST
/api/unified-inbox/conv-xlate-out	GET
/api/unified-inbox/conv-xlate-out	POST
/api/unified-inbox/quick-replies	POST
/api/unified-inbox/translate	POST
/api/unified-inbox/translate-compare	POST
/api/unified-inbox/translate-document	POST
/api/unified-inbox/translate-document-file	POST
/api/unified-inbox/translate-document-progress/{job_id}	GET
/api/unified-inbox/translated-file/{token}	GET
/api/unified-inbox/translate-image	POST
/api/unified-inbox/translate-message-media	POST
/api/unified-inbox/translate-video	POST
/api/unified-inbox/translate-voice	POST
/api/unified-inbox/translation-engines	GET
/api/workspace/claim	POST
/api/workspace/claim/release	POST
/api/workspace/claim/renew	POST
/api/workspace/claims	GET
/api/workspace/entrances	GET
/api/workspace/glossary	GET
/api/workspace/glossary	POST
/api/workspace/typing	POST
/api/workspace/contact/{contact_id}	GET
/api/workspace/contact/{contact_id}/crm	POST
/api/workspace/contact/{contact_id}/follow-up	POST
/api/workspace/contact/{contact_id}/tasks	GET
/api/workspace/contact/{contact_id}/timeline	GET
/api/workspace/contact/{contact_id}/origin	GET
/api/workspace/contact/{contact_id}/origin	POST
/api/workspace/conv/{conversation_id}/start-chain	POST
/api/workspace/workflow-chains	GET
/api/workspace/workflow-chains	POST
/api/workspace/workflow-chains/seed	POST
/api/workspace/workflow-chains/{chain_id}	PUT
/api/workspace/workflow-chains/{chain_id}	DELETE
/api/workspace/routing-rules	GET
/api/workspace/routing-rules	POST
/api/workspace/routing-rules/{rule_id}	PUT
/api/workspace/routing-rules/{rule_id}	DELETE
/api/workspace/routing-rules/evaluate	POST
/api/workspace/search	GET
/api/workspace/contact/{contact_id}/engagement	GET
/api/workspace/contact/{contact_id}/engagement	POST
/api/workspace/conv/{conversation_id}/reply-suggest	POST
/api/workspace/conv/{conversation_id}/copilot-prefill	GET
/api/workspace/conv/{conversation_id}/relationship-stage	GET
/api/workspace/conv/{conversation_id}/relationship-stage/confirm	POST
/api/workspace/conv/{conversation_id}/relationship-stage/downgrade	POST
/api/workspace/conv/{conversation_id}/relationship-stage/reunion	POST
/api/workspace/contact/{contact_id}/relationship-stage	GET
/api/workspace/contact/{contact_id}/relationship-stage/sync	POST
/api/workspace/contact/{contact_id}/stage-timeline	GET
/api/workspace/chain-executions	GET
/api/workspace/chain-funnel	GET
/api/workspace/conv/{conversation_id}/chain-executions	GET
/api/workspace/chain-executions/{exec_id}/cancel	POST
/api/workspace/chain-executions/{exec_id}/pause	POST
/api/workspace/chain-executions/{exec_id}/resume	POST
/api/workspace/chain-executions/{exec_id}/skip-step	POST
/api/workspace/chain-executions/{exec_id}/retry	POST
/api/workspace/conv/{conversation_id}/journey	GET
/api/workspace/conv/{conversation_id}/journey/stage	POST
/api/workspace/conv/{conversation_id}/deal	POST
/api/workspace/conv/{conversation_id}/deal/{deal_id}/revoke	POST
/api/workspace/workflows/deal-engine	GET
/api/workspace/workflows/deal-engine	POST
/api/workspace/workflow-chains/{chain_id}/bulk-start	POST
/api/workspace/journey-funnel	GET
/api/workspace/cta-targets	GET
/api/workspace/cta-targets	POST
/api/workspace/cta-targets/{target_id}	DELETE
/api/workspace/conv/{conversation_id}/cta-link	POST
/r/{token}	GET
/api/cta/convert	POST
/api/workspace/conv/{conversation_id}/mention-suggestions	GET
/api/workspace/conv/{conversation_id}/collab-context	GET
/api/workspace/contact/{contact_id}/collab-context	GET
/workspace/workflows	GET
/workflows	GET
/api/workspace/conv/{conversation_id}/qa-score	GET
/api/workspace/conv/{conversation_id}/qa-score	POST
/api/workspace/agent-qa-stats	GET
/api/workspace/churn-risks	GET
/api/workspace/contacts/export.csv	GET
/api/workspace/contacts/list	GET
/api/workspace/contacts/merge	POST
/api/workspace/contacts/merge-contact	POST
/api/workspace/contacts/overview	GET
/api/workspace/contacts/search	GET
/api/workspace/contacts/split	POST
/api/workspace/agent-frt-detail	GET
/api/workspace/daily-report.csv	GET
/api/workspace/dashboard	GET
/api/workspace/roi	GET
/api/workspace/ai-quality	GET
/api/workspace/usage	GET
/api/workspace/billing	GET
/api/setup/channels	GET
/api/setup/channels/{channel}	POST
/api/setup/checklist	GET
/api/setup/companion-preflight	GET
/api/setup/ai	GET
/api/setup/ai-key	POST
/api/setup/ai-primary	POST
/api/setup/ai-primary/audit	GET
/api/setup/deploy-profile	GET
/api/setup/cloud-credentials	GET
/api/setup/key-pool	POST
/api/setup/features	GET
/api/setup/features/toggle	POST
/api/workspace/ai-runtime-status	GET
/api/workspace/ai-weekly-brief	GET
/api/workspace/hosted-quota	GET
/api/workspace/channel-alert/mute	POST
/api/workspace/channel-alert/disable	POST
/api/companion/proactive/preview	GET
/api/companion/capabilities	GET
/api/companion/capabilities/delivery-calibration	GET
/api/companion/capabilities/realtime-voice-calibration	GET
/api/companion/capabilities/toggle	POST
/api/companion/capabilities/toggle-audit	GET
/api/companion/capabilities/preset	POST
/api/companion/capabilities/rollback	POST
/api/companion/standby	GET
/api/companion/standby	POST
/api/companion/capabilities/signals	GET
/api/companion/capabilities/advice	GET
/api/companion/media-capabilities	GET
/api/companion/media-capabilities/preset	POST
/api/companion/media-capabilities/provision	POST
/api/companion/quality-overview	GET
/api/companion/quality-trend	GET
/api/companion/proactive/sample	POST
/api/companion/proactive/sample/{sid}/rate	POST
/api/companion/proactive/samples	GET
/api/companion/proactive/tuning-advice	GET
/api/companion/proactive/status	GET
/api/workspace/agent-perf	GET
/api/workspace/agent-perf/timeline	GET
/api/workspace/agent-copilot-stats	GET
/api/workspace/escalation-log	GET
/api/workspace/escalation/{esc_id}/assign	POST
/api/workspace/escalations	GET
/api/workspace/escalations/mine	GET
/api/workspace/handoff-brief	GET
/api/workspace/conversation/{conversation_id}/seen-mention	POST
/api/workspace/conversation/{conversation_id}/snooze	POST
/api/workspace/conversation/{conversation_id}/snooze-history	GET
/api/workspace/conversation/{conversation_id}/unsnooze	POST
/api/workspace/snoozed	GET
/api/workspace/follow-up/{task_id}/assign	POST
/api/workspace/follow-up/{task_id}/done	POST
/api/workspace/follow-up/{task_id}/snooze	POST
/api/workspace/follow-ups	GET
/api/workspace/heartbeat	POST
/api/workspace/me	GET
/api/workspace/my-tasks	GET
/api/workspace/merge-reviews	GET
/api/workspace/merge-reviews/{review_id}	POST
/api/workspace/metrics/web-funnel	GET
/api/workspace/presence	GET
/api/workspace/presence	POST
/api/workspace/prefs	GET
/api/workspace/prefs	POST
/api/workspace/sla-alerts	GET
/api/workspace/sla-detail	GET
/api/workspace/sla/create-task	POST
/api/workspace/stream	GET
/api/workspace/conv/{conversation_id}/archive	PATCH
/api/workspace/conv/{conversation_id}/summarize	POST
/api/workspace/conv/{conversation_id}/tags	GET
/api/workspace/conv/{conversation_id}/tags	PUT
/api/workspace/tag-library	GET
/api/workspace/tag-library	POST
/api/workspace/tag-library/{tag}	DELETE
/api/workspace/tag-stats	GET
/api/workspace/tags	GET
/api/workspace/batch/archive	POST
/api/workspace/batch/tags	POST
/api/workspace/batch/assign	POST
/api/workspace/notifications	GET
/api/workspace/notifications/read	POST
/api/workspace/notifications/sys-status	POST
/api/workspace/conv/{conversation_id}/notes	GET
/api/workspace/conv/{conversation_id}/notes	POST
/api/workspace/conv/{conversation_id}/notes/{note_id}	PATCH
/api/workspace/conv/{conversation_id}/notes/{note_id}	DELETE
/api/workspace/activity-heatmap	GET
/api/workspace/queue-monitor	GET
/api/workspace/queue-monitor/reassign	POST
/api/workspace/webhook-outbound	GET
/api/workspace/webhook-outbound/test	POST
/api/user-segments	GET
/api/users/at-risk	GET
/api/vision-stats	GET
/api/voice/cloned	GET
/api/voice/effective-config	GET
/api/voice/enroll	POST
/api/voice/persona-audit	GET
/api/voice/preset-catalog	GET
/api/voice/profiles	GET
/api/voice/profiles/{persona_id}	DELETE
/api/voice/purge	POST
/api/voice/purge-orphans	POST
/api/voice/rebind	POST
/api/voice/reconcile	GET
/api/voice/tts-file/{filename}	GET
/api/voice/tts-test	POST
/api/voice/tts-test/{filename}	GET
/api/voice/tts-test-jobs/{job_id}	GET
/api/singing/overview	GET
/api/singing/audio/{persona_id}/{template_id}	GET
/api/singing/anchor/{voice_key}	GET
/api/singing/config	POST
/api/singing/template-enable	POST
/api/singing/orders	GET
/api/singing/orders/{oid}/audio	GET
/api/singing/orders/{oid}/approve	POST
/api/singing/orders/{oid}/reject	POST
/api/singing/orders/{oid}/retry	POST
/api/singing/orders/{oid}/delete	POST
/api/singing/supply-request	POST
/api/singing/supply-status	GET
/api/workspace/channel-sessions	GET
/api/webhook-settings	GET
/api/webhook-settings	PUT
/api/webhook-test	POST
/api/whatsapp-rpa/accept-contacts	POST
/api/whatsapp-rpa/alerts	GET
/api/whatsapp-rpa/alerts/ack_all	POST
/api/whatsapp-rpa/alerts/{alert_id}/ack	POST
/api/whatsapp-rpa/chat-blacklist	POST
/api/whatsapp-rpa/chat-history	GET
/api/whatsapp-rpa/chat-history/{chat_key:path}	GET
/api/whatsapp-rpa/chat-lang-lock	POST
/api/whatsapp-rpa/chat-quiet	POST
/api/whatsapp-rpa/config	GET
/api/whatsapp-rpa/config	PUT
/api/whatsapp-rpa/conversations	GET
/api/whatsapp-rpa/customer-profile/{chat_key:path}	GET
/api/whatsapp-rpa/device-screenshot	GET
/api/whatsapp-rpa/intent-stats	GET
/api/whatsapp-rpa/log-tail	GET
/api/whatsapp-rpa/media-metrics	GET
/api/whatsapp-rpa/pause	POST
/api/whatsapp-rpa/pending	GET
/api/whatsapp-rpa/pending-tts	GET
/api/whatsapp-rpa/pending/cancel-all	POST
/api/whatsapp-rpa/pending/{pending_id}/resolve	POST
/api/whatsapp-rpa/pending/{pending_id}/retry-tts	POST
/api/whatsapp-rpa/pipeline-metrics	GET
/api/whatsapp-rpa/proactive-metrics	GET
/api/whatsapp-rpa/proactive-stats	GET
/api/whatsapp-rpa/recent	GET
/api/whatsapp-rpa/reset-circuit-breaker	POST
/api/whatsapp-rpa/resume	POST
/api/whatsapp-rpa/search	GET
/api/whatsapp-rpa/send-manual	POST
/api/whatsapp-rpa/send-queue	GET
/api/whatsapp-rpa/send-queue/{item_id}	GET
/api/whatsapp-rpa/send-queue/{item_id}/cancel	POST
/api/whatsapp-rpa/sessions/{chat_key:path}	GET
/api/whatsapp-rpa/status	GET
/api/whatsapp-rpa/template-analytics	GET
/api/whatsapp-rpa/timeline	GET
/api/whatsapp-rpa/trigger	POST
/api/whatsapp-rpa/tts-test	POST
/api/whatsapp-rpa/voice-metrics	GET
/audit	GET
/audit/export	GET
/cases	GET
/channels	GET
/channels/update	POST
/developer	GET
/developer/auth	POST
/developer/logout	POST
/care-schedule	GET
/crisis-audit	GET
/relations-health	GET
/monetization	GET
/diff	GET
/episodic-memory	GET
/episodic_memory	GET
/export	GET
/health	GET
/help	GET
/i18n/ws-i18n.js	GET
/import	GET
/import	POST
/kb-images/{filename}	GET
/knowledge	GET
/learner	GET
/line-rpa	GET
/login	GET
/login	POST
/logout	GET
/logs	GET
/logs/stream	GET
/messenger-rpa	GET
/openapi.json	GET
/personal-settings	GET
/personas	GET
/reply-settings	GET
/rpa-overview	GET
/set_lang	GET
/set_ui_mode	GET
/settings	GET
/setup	GET
/singing	GET
/strategies	GET
/strategy-analytics	GET
/telegram	GET
/templates	GET
/templates/update	POST
/training	GET
/unified-inbox	GET
/workspace	GET
/workspace/agent-perf	GET
/workspace/queue	GET
/workspace/contact/{contact_id}	GET
/workspace/contacts	GET
/workspace/dash	GET
/api/reply-templates	GET
/api/workspace/export	GET
/api/workspace/metrics	GET
/api/workspace/report	GET
/api/workspace/broadcast	POST
/api/workspace/leaderboard	GET
/api/workspace/trend	GET
/api/workspace/my-perf	GET
/api/workspace/kb-archive	POST
/api/workspace/workspaces	GET
/api/workspace/workspaces	POST
/api/workspace/workspaces/{workspace_id}/stats	GET
/api/workspace/kb-stats	GET
/api/workspace/kb-click	POST
/api/workspace/quality-stats	GET
/api/workspace/workload	GET
/api/reply-templates	POST
/api/reply-templates/{template_id}	DELETE
/api/reply-templates/{template_id}	PUT
/api/reply-templates/{template_id}/use	POST
/api/unified-inbox/conv-meta	GET
/api/unified-inbox/contact-profile	GET
/workspace/draft-audit	GET
/workspace/drafts	GET
/workspace/templates	GET
/workspace/escalations	GET
/workspace/roi	GET
/workspace/setup	GET
/workspace/channels	GET
/workspace/channels/{channel}	GET
/workspace/kb-start	GET
/workspace/golive	GET
/workspace/ai-quality	GET
/workspace/usage	GET
/workspace/tasks	GET
/users	GET
/users/create	POST
/users/delete/{user_id}	POST
/users/update/{user_id}	POST
/users/quota/{user_id}	POST
/users/perms/{user_id}	POST
/users/notify-binding/{user_id}	POST
/api/workspace/my-notify-binding	GET,POST
/api/workspace/my-notify-binding/test	POST
/api/users/{user_id}/perms	GET
/api/users/char-usage	GET
/api/workspace/my-usage	GET
/whatsapp-rpa	GET
/api/workspace/ab-tests	GET,POST
/api/workspace/ab-tests/{test_id}/results	GET
/api/workspace/ab-tests/{test_id}/stop	POST
/api/workspace/anomaly	GET
/api/workspace/trace	GET
/api/workspace/trace/{trace_id}	GET
/ops/voice-call	GET
/api/voice/engine/status	GET
/api/voice/engine/load	POST
/api/voice/engine/unload	POST
/api/voice/local-tts/status	GET
/api/voice/local-tts/toggle	POST
/api/voice/avatar-status	GET
/api/voice/prerender-lines	GET
/api/voice/prerender-lines/add	POST
/api/voice/prerender-lines	GET
/api/voice/prerender-lines/add	POST
/api/voice/live/readiness	GET
/api/voice/call/readiness	GET
/api/voice/personas	GET
/api/voice/conversations	GET
/api/voice/preview	POST
/api/voice/persona-voice	POST
/api/voice/persona-voice	DELETE
"""

# 2026-07-20 基线增量（拆分完成后的**有意新增**端点，非搬迁；随功能提交入册）：
# - enable/leadbus/replybus：平台化总线（platform/* 合同对接：授权/线索/回复决策）
# - monetize/kpi：概览页经营摘要条（三期）  - ops/account-health：机群健康灯
# - personas/face-refs：人设形象照参照   - platforms/login/password：协议号密码登录补全
# - telegram/gate-stats：反封号闸门观测   - translate：通用翻译端点
_ADDITIONS_2026_07 = """
/api/enable/status	GET
/api/leadbus/ingest	POST
/api/leadbus/status	GET
/api/monetize/kpi	GET
/api/ops/account-health	GET
/api/personas/face-refs	GET
/api/platforms/{platform}/login/{login_id}/password	POST
/api/replybus/decide	POST
/api/replybus/status	GET
/api/telegram/gate-stats	GET
/api/translate	POST
/funnel	GET
"""
_BASELINE += _ADDITIONS_2026_07

# 2026-07-22 基线增量：Telegram 账号级聊天记录同步（对齐手机——云端 get_dialogs +
# get_chat_history 直写 store；POST 触发后台同步 / GET 轮询进度）。
_ADDITIONS_2026_07_22 = """
/api/platforms/telegram/{account_id}/sync-history	GET
/api/platforms/telegram/{account_id}/sync-history	POST
"""
_BASELINE += _ADDITIONS_2026_07_22

# 2026-08-02: Telegram 单会话「深度回填」（POST 从库中最旧锚点向云端连续拉到
# 上限/到头，流式分批落库跑在 pyrogram loop；GET 轮询进度）——/history 单页
# ≤200 条的深历史补全升级，详见 unified_inbox_account_routes。
_ADDITIONS_2026_08_02 = """
/api/platforms/telegram/{account_id}/deep-backfill	GET
/api/platforms/telegram/{account_id}/deep-backfill	POST
"""
_BASELINE += _ADDITIONS_2026_08_02

# 2026-07-22 读路径 P0：坐席打开会话即写「已读水位」(last_read_ts) 落库，
# 读路径据此派生有效未读、永不回弹（unified_inbox_read_routes.py）。
_ADDITIONS_2026_07_22_READ = """
/api/unified-inbox/mark-read	POST
"""
_BASELINE += _ADDITIONS_2026_07_22_READ

# 2026-07-22 账号官方资料修改（accounts.profile_push，P1 写方向）：GET 详情
# （能力/自身资料/冷却/审计）+ POST 推送昵称/签名/头像到平台官方
# （Telegram 协议直改 / WhatsApp 经 Baileys；unified_inbox_account_routes.py）。
_ADDITIONS_2026_07_22_PROFILE = """
/api/accounts/{platform}/{account_id}/profile	GET
/api/accounts/{platform}/{account_id}/profile	POST
/api/accounts/persona-align	POST
"""
_BASELINE += _ADDITIONS_2026_07_22_PROFILE

# 2026-07-23 融合实例 P3/P4b：会员中心（档位/功能矩阵/用量/到期 + nav 锁标与
# 顶栏徽章的落点；membership_routes.py，数据口径=feature_gate.gate_snapshot 单源）
# + charpack 字符加量包入账（quota_store.add_license_topup，ref 幂等）。
_ADDITIONS_2026_07_23_MEMBERSHIP = """
/membership	GET
/api/admin/membership	GET
/api/admin/license/topup	POST
"""
_BASELINE += _ADDITIONS_2026_07_23_MEMBERSHIP

# 2026-07-24 融合实例 P4c：字符加量凭证自助兑换（topup_voucher.redeem——验签+
# 绑定校验+ref 幂等入账；charpack 自动履约闭环的客户侧终点）。
_ADDITIONS_2026_07_24_TOPUP_VOUCHER = """
/api/admin/license/topup-voucher	POST
"""
_BASELINE += _ADDITIONS_2026_07_24_TOPUP_VOUCHER

# 2026-07-25 账号隔离健康观测：多协议号数据分桶（记忆键形态）+ 在线号人设绑定 +
# FateX 独立库备货，一屏可见（ops_overview_routes.py，只读 + 60s TTL 缓存）。
_ADDITIONS_2026_07_25_ISOLATION = """
/api/admin/isolation-health	GET
"""
_BASELINE += _ADDITIONS_2026_07_25_ISOLATION

# 2026-07-25 群脉 CrowdX 导演控制台：剧本库 + 逐拍详情 + 一键离线排练（dry-run，
# 不发任何真消息）+ 历史场次 + 关联风险体检（group_show_routes.py，页面 /group-show）。
# linkage 是只读体检：排练永远用占位号，这个接口回答「换成真号最多能上几个」。
_ADDITIONS_2026_07_25_GROUP_SHOW = """
/api/group-show/attendance	POST
/api/group-show/attendance/joined	POST
/api/group-show/exposure	GET
/api/group-show/live	POST
/api/group-show/linkage	GET
/api/group-show/outcomes	GET
/api/group-show/playbooks	GET
/api/group-show/playbooks/{pid}	GET
/api/group-show/rehearse	POST
/api/group-show/schedule	POST
/api/group-show/sessions	GET
/group-show	GET
"""
_BASELINE += _ADDITIONS_2026_07_25_GROUP_SHOW

# 2026-07-25 前端 UI 交互埋点 beacon（空态引导按钮点击率/群区模式切换等，任意登录
# 用户可写；UiEventStats 进程级计数，读出走 workspace metrics.ui_events +
# Prometheus ui_events_*；drafts_routes.py::register_telemetry_route）。
_ADDITIONS_2026_07_25_UI_EVENT = """
/api/telemetry/ui-event	POST
"""
_BASELINE += _ADDITIONS_2026_07_25_UI_EVENT

# 2026-07-27 人设有效解析 + 账号人设绑定（persona_routes.py）：坐席侧读「此刻生效人设」
# 与写「账号→人设」映射；与群脉真发选角共用同一份 persona 解析口径。
_ADDITIONS_2026_07_27_PERSONA_EFFECTIVE = """
/api/persona/account-persona	POST
/api/persona/effective	GET
"""
_BASELINE += _ADDITIONS_2026_07_27_PERSONA_EFFECTIVE

# 2026-07-27 P2 注册领试用（license_routes.py）：官网按机器码建单 → 厂商机离线签发
# → 客户端轮询取回并**自动落盘激活**；bind-code 出「加客服领字符」深链，客服核销后
# 同一轮询把加量凭证入账。三端点都不抛（领取失败仍可退回粘贴授权码那条老路）。
_ADDITIONS_2026_07_27_TRIAL_CLAIM = """
/api/admin/license/trial-bind-code	POST
/api/admin/license/trial-claim	GET
/api/admin/license/trial-claim	POST
"""
_BASELINE += _ADDITIONS_2026_07_27_TRIAL_CLAIM

# 2026-07-28 首启漏斗埋点（license_routes.py）：向导曝光/领取/跳过事件经本地后端
# 转发官网 /api/track（事件白名单在 trial_claim_client.FUNNEL_EVENTS 收口）。
_ADDITIONS_2026_07_28_TRIAL_FUNNEL = """
/api/admin/license/trial-funnel	POST
"""
_BASELINE += _ADDITIONS_2026_07_28_TRIAL_FUNNEL

# 2026-08-11 邀请裂变（license_routes.py）：会员页「邀请好友送字符」卡片数据
# （我的邀请码/分享链接/进度统计，数据源=官网 referral 台账 invite-info）+
# ops「🎁 邀请裂变」卡的官网全局聚合代理（licensing.trial.referral_stats 默认关，
# 300s TTL；聚合是全站口径，只有厂商 ops 实例该开）。
_ADDITIONS_2026_08_11_REFERRAL = """
/api/admin/license/referral	GET
/api/admin/referral-stats	GET
"""
_BASELINE += _ADDITIONS_2026_08_11_REFERRAL

# 2026-07-27 营销目标（goal_routes.py）：会话级「工作目标」（付费转化/关系推进/沉默唤回…）
# CRUD + settle-on-read 视图。右栏卡/看板/prompt 注入三个消费面读同一份结算口径
# （service.refresh_goal）；companion.goals.enabled 关闭时全端点 403。
_ADDITIONS_2026_07_27_GOALS = """
/api/goals	GET,POST
/api/goals/templates	GET
/api/goals/agenda	GET
/api/goals/for-conversation	GET
/api/goals/{goal_id}	GET
/api/goals/{goal_id}/update	POST
/api/goals/{goal_id}/status	POST
"""
_BASELINE += _ADDITIONS_2026_07_27_GOALS

# P2（同日）：结果闭环报表 + 批量 campaign + 坐席今日拍反馈（采纳/驳回回流 planner）。
_ADDITIONS_2026_07_27_GOALS_P2 = """
/api/goals/report	GET
/api/goals/batch	POST
/api/goals/{goal_id}/beat/feedback	POST
"""
_BASELINE += _ADDITIONS_2026_07_27_GOALS_P2

# 获客转化漏斗（同日 P1）：客户画像卡（双轨槽位 GET + 坐席手录 POST；
# auto 采集只填空槽，agent 手录覆盖一切）。
_ADDITIONS_2026_07_27_GOALS_PROFILE = """
/api/goals/profile	GET,POST
/api/goals/order-hook	POST
"""
_BASELINE += _ADDITIONS_2026_07_27_GOALS_PROFILE

# P10：开闸就绪度（goals 关也 200——正是「为什么跑不起来」的答案）。
_ADDITIONS_2026_07_27_GOALS_READINESS = """
/api/goals/readiness	GET
"""
_BASELINE += _ADDITIONS_2026_07_27_GOALS_READINESS

# P3 2026-08-30 冲刺推进器：坐席「立即推进」——冲刺目标当场排一条主动拍
# （节奏闸对人工触发不适用；危机/opt-out/会话档位等安全闸原样过）。
_ADDITIONS_2026_08_30_GOALS_SPRINT_NUDGE = """
/api/goals/{goal_id}/sprint/nudge	POST
"""
_BASELINE += _ADDITIONS_2026_08_30_GOALS_SPRINT_NUDGE

# #166 J-2 2026-09-05：目标引擎真相（goals 开关 / 注入 / 冲刺推进器 + 派发终点
# care 关闸·dry_run / 平台白名单 / 自然档主动桥）——前端据此不再兜售引擎不执行
# 的「自动推进」。goals 关也 200（与 readiness 同哲学：关着也要能说明为什么）。
_ADDITIONS_2026_09_05_GOALS_ENGINE_STATUS = """
/api/goals/engine-status	GET
"""
_BASELINE += _ADDITIONS_2026_09_05_GOALS_ENGINE_STATUS

# D1b P0-4 2026-09-05：单目标预检——引擎配置 / 进程活性 / 目标运行时闸（会话档位·
# 危机·opt-out，与 ticker run_once 同一函数）三层合一，卡片只在全过时承诺下一拍。
_ADDITIONS_2026_09_05_GOALS_PREFLIGHT = """
/api/goals/{goal_id}/preflight	GET
"""
_BASELINE += _ADDITIONS_2026_09_05_GOALS_PREFLIGHT

# M-7 A 2026-09-07（#236 = #166 族第五次）：目标「每一拍」清单——主动真发（经 care 行
# 反查话术与 deferred 投递真相 + 出站消息 id 可跳转）/ 回复链带方向 / 想出手被拦及
# 原因 / 首拍待预览；卡片「已推进 N 拍」点开读的就是它，与看门狗 sent_24h 同口径。
_ADDITIONS_2026_09_07_GOALS_BEATS = """
/api/goals/{goal_id}/beats	GET
"""
_BASELINE += _ADDITIONS_2026_09_07_GOALS_BEATS

# O-3 E 2026-09-08（#236 #257 HM7XBA）：摸底目标「现在就问一个」——预览未填槽问法
# （槽位 / 问法 / 示例 / 拦截原因）+ 点发即发（排 goal:{gid}:q{ts} care 行 → 派发器
# send_now 同一条守卫 / 拟稿 / 投递链 → beat_sent 计入「主动出手」+ 出站文本当场校验）。
_ADDITIONS_2026_09_08_GOALS_PROBE = """
/api/goals/{goal_id}/probe/preview	GET
/api/goals/{goal_id}/probe/send	POST
"""
_BASELINE += _ADDITIONS_2026_09_08_GOALS_PROBE

# 实施91（小智线 impl88）PC 受控机管理三端点——**代登记**（2026-08-30 晚：
# 该线路由已落盘、清单行未及登记，红了全树装配门禁并挡住老板点名的发版重启；
# 语义归属仍是小智线，端点行为以其实现为准，见 .ops NOTE_from_goals_sprint_*）。
_ADDITIONS_2026_08_30_ASSISTANT_PC_PROXY = """
/api/assistant/pc/machines	GET
/api/assistant/pc/restore	POST
/api/assistant/pc/revoke	POST
"""
_BASELINE += _ADDITIONS_2026_08_30_ASSISTANT_PC_PROXY

# 实施91 P2-C 信任档（免确认连跑，计时授信/急停）
_ADDITIONS_2026_08_31_ASSISTANT_PC_TRUST = """
/api/assistant/pc/trust	POST
/api/assistant/pc/trust/revoke	POST
"""
_BASELINE += _ADDITIONS_2026_08_31_ASSISTANT_PC_TRUST

# 2026-08-18：完成通知链路状态（目标卡「达成后会通知谁」可见化；零密钥，
# 文件真相口径与告警渠道面板同源）。
_ADDITIONS_2026_08_18_GOALS_NOTIFY_STATUS = """
/api/goals/notify-status	GET
"""
_BASELINE += _ADDITIONS_2026_08_18_GOALS_NOTIFY_STATUS

# 2026-08-18 P3：完成推送设置写口（扫描器/坐席副本/失守日报/画像出境四开关，
# set_overlay_flag 保注释写 overlay 热生效；路径硬编码白名单，拒 agent/viewer）
# ——此前扫描器开关只能改 YAML，是推送设置面最后一块无 UI 死角。
_ADDITIONS_2026_08_18_GOALS_NOTIFY_SETTINGS = """
/api/goals/notify-settings	POST
"""
_BASELINE += _ADDITIONS_2026_08_18_GOALS_NOTIFY_SETTINGS

# 2026-07-27 人设上线前质检（E 线统一登记两家）：quiz=档案自动出题→真人设 prompt
# 逐题实测→自动判分（persona_quiz_routes.py，任务经 persona_doc_import 注册表轮询）；
# bio-doc=D2 线「人设传记文档」三端点（契约固定先行入册，路由实现由 D2 落地）。
_ADDITIONS_2026_07_27_PERSONA_QUIZ_BIO = """
/api/personas/quiz/status	GET
/api/personas/{profile_id}/quiz	POST
/api/personas/{profile_id}/quiz/jobs/{job_id}	GET
/api/personas/{profile_id}/retired-quiz	POST
/api/personas/{profile_id}/bio-doc	POST
/api/personas/{profile_id}/bio-doc	GET
/api/personas/{profile_id}/bio-doc	DELETE
"""
_BASELINE += _ADDITIONS_2026_07_27_PERSONA_QUIZ_BIO

# 2026-07-27 H3：考题报告持久化历史 API
_ADDITIONS_2026_07_27_PERSONA_QUIZ_REPORTS = """
/api/personas/{profile_id}/quiz/reports	GET
/api/personas/{profile_id}/quiz/reports/{report_id}	GET
"""
_BASELINE += _ADDITIONS_2026_07_27_PERSONA_QUIZ_REPORTS

# 2026-07-28 人设质检续（I3 线统一登记两家）：quiz/trend=考题跨进程趋势（读 quiz.db，
# flag 关也 200 供 ops 卡显隐判断）；bio-doc/search=I1 线「传记片段检索」（契约逐字固定
# 先行入册，路由实现由 I1 落地）。
_ADDITIONS_2026_07_28_PERSONA_QUIZ_TREND = """
/api/personas/quiz/trend	GET
/api/personas/{profile_id}/bio-doc/search	POST
"""
_BASELINE += _ADDITIONS_2026_07_28_PERSONA_QUIZ_TREND

# 2026-07-28 P17 营销目标「今日工作清单」（goal_routes.py）：把看板的计数变成坐席能
# 照着干的点名单（今天该推谁 / 谁还没人审 / 谁让路了）。只读端点（viewer 也能看），
# 逐条走 GET /api/goals 同一条 settle-on-read 口径，不另开第二条结算路。
# 反馈撤销（verdict=undo）与手动标成交的可选归因 meta 都挂在既有端点上，故只增一条。
_ADDITIONS_2026_07_28_GOALS_AGENDA = """
/api/goals/agenda	GET
"""
_BASELINE += _ADDITIONS_2026_07_28_GOALS_AGENDA

# 2026-07-28 J1：长传记「补齐向量」运维口——句向量改入库期预计算后，早于该改动入库的
# 传记库整表无句向量、检索静默退化成纯关键词；本端点给缺向量的块/句补嵌（后台任务，
# 轮询复用既有 /api/personas/import-doc/jobs/{job_id}，故此处只增一条）。
_ADDITIONS_2026_07_28_PERSONA_BIO_REEMBED = """
/api/personas/{profile_id}/bio-doc/reembed	POST
"""
_BASELINE += _ADDITIONS_2026_07_28_PERSONA_BIO_REEMBED

# 2026-07-28 M9 收尾：传记库存盘点（只读）——库存×档案×账号绑定三维对齐视图，
# `orphans`（有库存无档案）供 Studio 顶部横幅消费；误删/平行建档两类错位从翻库
# 变成一眼可见。
_ADDITIONS_2026_07_28_PERSONA_BIO_INVENTORY = """
/api/personas/bio-inventory	GET
"""
_BASELINE += _ADDITIONS_2026_07_28_PERSONA_BIO_INVENTORY

# 2026-08-31：多模型路由管理（像 Cursor 按任务挑模型/端点；GET 总览 + POST 保存 overlay）
_ADDITIONS_2026_08_31_MODEL_ROUTES_INVENTORY = """
/api/setup/model-routes	GET
/api/setup/model-routes	POST
"""
_BASELINE += _ADDITIONS_2026_08_31_MODEL_ROUTES_INVENTORY

# 2026-07-28 M10：导入向导「保存并验收」流水线——人工审核闸后的机械尾巴
# （建档同步 + 传记入库/考题验收后台任务）收成一次提交；轮询复用
# /api/personas/import-doc/jobs/{job_id}。账号绑定刻意不在流水线内（运营决策）。
_ADDITIONS_2026_07_28_PERSONA_FINALIZE = """
/api/personas/import-doc/finalize	POST
"""
_BASELINE += _ADDITIONS_2026_07_28_PERSONA_FINALIZE

# 2026-07-31 P2：浏览器环境体检（人设切换事故产品化）——echo=写通道零副作用探针
# （过完整鉴权+CSRF 链，回显放行通行证）；summary=时钟/构建戳/身份/AI 降级态；
# csrf-trend=准入/拒绝日趋势（同源回落收口决策的数据面）。
_ADDITIONS_2026_07_31_PREFLIGHT_CSRF_TREND = """
/api/preflight/echo	POST
/api/preflight/summary	GET
/api/admin/csrf-trend	GET
"""
_BASELINE += _ADDITIONS_2026_07_31_PREFLIGHT_CSRF_TREND

# 2026-07-31 告警分层产品化：按受众分组的告警目录（终端大白话勾选、不再手打别名）。
_ADDITIONS_2026_07_31_ALERT_CATALOG = """
/api/accounts/auto-reply/alert-catalog	GET
"""
_BASELINE += _ADDITIONS_2026_07_31_ALERT_CATALOG

# 2026-08-01 P23 质量抽检：坐席指令→拟稿产出留样尾窗（无客户原文；周审
# growth_review --samples 消费）。样本由回复台指令链产生，不依赖 goals 开关
# → 与 readiness 同豁免不走 _require_enabled。
_ADDITIONS_2026_08_01_INSTR_SAMPLES = """
/api/goals/instr-samples	GET
"""
_BASELINE += _ADDITIONS_2026_08_01_INSTR_SAMPLES

# 2026-08-02 客户资产趋势：好友/未开口/沉默按日快照（写侧=看板读时懒快照；
# 破冰/主动触达上线后「未开口存量有没有压下去」的验收判据）。
_ADDITIONS_2026_08_02_CONTACTS_ASSET_TREND = """
/api/admin/contacts-asset-trend	GET
"""
_BASELINE += _ADDITIONS_2026_08_02_CONTACTS_ASSET_TREND

# 2026-08-03 案例中心 P4：案例认领/释放（多坐席分工——谁在跟、别重复跟；
# body {"release": bool}，审计 case_claim/case_release）。
_ADDITIONS_2026_08_03_CASE_CLAIM = """
/api/cases/{case_id}/claim	POST
"""
_BASELINE += _ADDITIONS_2026_08_03_CASE_CLAIM

# 2026-08-04 媒体按需拉取 P1：历史/超限/存量占位行的「拉取原件」——按 store 行的
# platform_msg_id 经 pyrogram get_messages + download_tg_media 归档并回填 media_ref
# （update_message_media 幂等）。配置闸 telegram.media_fetch（默认关）+ 行级单飞
# + 全局并发上限；body {"message_id"}。
_ADDITIONS_2026_08_04_FETCH_MEDIA = """
/api/platforms/telegram/{account_id}/fetch-media	POST
/api/platforms/line/{account_id}/fetch-media	POST
"""
_BASELINE += _ADDITIONS_2026_08_04_FETCH_MEDIA

# 2026-08-05 全量深同步 P2：战役式把账号云端全部会话深历史吸进工作台（热会话
# 优先、per_chat×total 双预算、断点续跑；引擎 src/integrations/tg_full_sync.py）。
# POST 触发（body {"restart": bool}）/ GET 轮询进度+断点账本摘要。
# 配置闸 telegram.full_sync（默认关）；与账号级 sync-history 双向互斥。
_ADDITIONS_2026_08_05_FULL_SYNC = """
/api/platforms/telegram/{account_id}/full-sync	GET,POST
"""
_BASELINE += _ADDITIONS_2026_08_05_FULL_SYNC

# 2026-08-07 托管租户观测面：实例状态/持单台账/三守护心跳（ops「☁️ 托管租户」卡；
# 收集器 src/ops/tenant_overview.py，30s TTL；active=false 前端整卡隐藏）。
_ADDITIONS_2026_08_07_TENANT_OVERVIEW = """
/api/admin/tenant-overview	GET
"""
_BASELINE += _ADDITIONS_2026_08_07_TENANT_OVERVIEW

# 2026-08-09 目标达成报表（P0）：账号×客户完成情况（matrix=哪个号在出成绩 /
# contacts=完成客户清单＝销售线索，含「完成后是否已跟进」客观推导）+ 主管报表页。
# 配套：goals.sweep 定时结算 + goals.notify 完成提醒（goal_completed_alert →
# 铃铛/toast/webhook 别名 goal_complete）。
_ADDITIONS_2026_08_09_GOAL_REPORT = """
/api/goals/report/accounts	GET
/api/goals/report/contacts	GET
/workspace/goal-report	GET
"""
_BASELINE += _ADDITIONS_2026_08_09_GOAL_REPORT

# 2026-08-09 坐席翻译提速批次：批量翻译端点——收件箱视口懒翻从「N 条消息 N 个
# HTTP 往返」收成一次请求（逐条复用与 /translate 同一 TranslationService：缓存/
# 术语/会话首选引擎；服务端 gather + 信号量 8 防 GPU 队列被突发塞满）。
_ADDITIONS_2026_08_09_TRANSLATE_BATCH = """
/api/unified-inbox/translate-batch	POST
"""
_BASELINE += _ADDITIONS_2026_08_09_TRANSLATE_BATCH

# P2 2026-08-19「问这张图」：坐席对入站图片即席提问（类型感知预设 + 自由输入），
# VisionClient 带问题重问该图；unified_inbox_vision_routes.register_vision_routes。
_ADDITIONS_2026_08_19_ASK_IMAGE = """
/api/unified-inbox/ask-image	POST
"""
_BASELINE += _ADDITIONS_2026_08_19_ASK_IMAGE

# 2026-08-10 FB Messenger Webhook 常驻挂载：SkillManager 改为请求期解析
# （telegram_client → app.state 双兜底），协议号未配置的部署（telegram_client
# =None）路由也照常挂载——未就绪回 503 由 Meta 重投。见 facebook_webhook.py
# 模块 docstring（2026-08-10 搭车验证实锤：注册期取不到 → /fb/webhook 404）。
_ADDITIONS_2026_08_10_FB_WEBHOOK = """
/fb/webhook	GET
/fb/webhook	POST
"""
_BASELINE += _ADDITIONS_2026_08_10_FB_WEBHOOK

# 2026-08-10 API_ID_INVALID 事故链 P1：
# - preflight：接入弹窗打开即探「本机能否直连 Telegram DC」（TCP 快败 + 60s 缓存），
#   直连不通提前亮黄条给配代理路标（只提示不阻断；src/integrations/tg_preflight.py）。
# - diagnostic-upload：一键诊断直传——本机打包（密钥打码）→ 后端转投官网
#   /api/diag-upload → 回 6 位短码给客服（浏览器直传会撞 CORS，故走 server-to-server）。
_ADDITIONS_2026_08_10_ONBOARDING_RESCUE = """
/api/platforms/telegram/login/preflight	GET
/api/admin/diagnostic-upload	POST
"""
_BASELINE += _ADDITIONS_2026_08_10_ONBOARDING_RESCUE

# 2026-08-10 Telegram 手机号+验证码登录：submit/resend 与扫码的 /password 并列。
_ADDITIONS_2026_08_10_PHONE_CODE = """
/api/platforms/{platform}/login/{login_id}/code	POST
/api/platforms/{platform}/login/{login_id}/resend-code	POST
"""
_BASELINE += _ADDITIONS_2026_08_10_PHONE_CODE

# 2026-08-12 Telegram 群成员提取（工具箱）：多号限速拉群成员入库 + 每日配额（group_members_routes）。
_ADDITIONS_2026_08_12_TG_MEMBERS = """
/api/tg-members/jobs	POST
/api/tg-members/jobs	GET
/api/tg-members/jobs/{job_id}	GET
/api/tg-members/jobs/{job_id}/stop	POST
/api/tg-members/groups	GET
/api/tg-members/members	GET
/api/tg-members/members/export	GET
/api/tg-members/quota	GET
/api/tg-members/account-groups	GET
/tools/tg-members	GET
"""
_BASELINE += _ADDITIONS_2026_08_12_TG_MEMBERS

# 2026-08-21 工具箱新增「AI 生成图片」（image_gen_routes）+「智能养号」（nurture_routes）。
# 前者复用 SelfieProvider+image_gate+comfy_infer 现场出图；后者状态复用 fleet-health、
# 配置写 ops.nurture overlay（执行引擎属下一阶段）。
_ADDITIONS_2026_08_21_TOOLBOX = """
/api/image/config	GET
/api/image/generate	POST
/api/image/save-album	POST
/api/nurture/status	GET
/api/nurture/config	GET
/api/nurture/config	POST
/api/nurture/engine	POST
/api/nurture/shadow	GET
/api/nurture/probe	POST
"""
_BASELINE += _ADDITIONS_2026_08_21_TOOLBOX

# 2026-08-22 手动出图 P1（image_gen_routes 增量）：异步任务三件套（同步长连接
# 挂 2-5 分钟不可取消 → 任务化+ComfyUI /interrupt 配合）+ 相册优先层（生成前
# 秒查存货，零 GPU 即取即发；album-file=相册只读文件服务，路径钉死 album_dir
# 子树）+ mark-sent（发送回写 persona_media_sends——重发冷却/服装连续窗从此
# 认得手动发出的图，一致性旁路收口）。
_ADDITIONS_2026_08_22_IMAGE_P1 = """
/api/image/jobs	POST
/api/image/jobs/{job_id}	GET
/api/image/jobs/{job_id}/cancel	POST
/api/image/album-stock	GET
/api/image/album-file	GET
/api/image/mark-sent	POST
/api/image/scene-hints	GET
"""
_BASELINE += _ADDITIONS_2026_08_22_IMAGE_P1

# 2026-08-12 被骂回怼治理（temper_routes）：治理配置读写 + 每人设生效档位 +
# 回怼诊断器 dry-run（「为什么没怼」运营自查，只读零副作用）。
_ADDITIONS_2026_08_12_TEMPER = """
/api/companion/temper/status	GET
/api/companion/temper/config	POST
/api/companion/temper/dry-run	POST
"""
_BASELINE += _ADDITIONS_2026_08_12_TEMPER

# 2026-08-13 双面板融合 P0（surface_fusion_routes）：能力注册表（workspace ×
# native 双面板状态 + 桥接目标）+ 驾驶权互斥锁读/切（防「工作台自动链 ×
# 原生页」双发的机制层；owner=native 时 AutosendWorker 让位）。
_ADDITIONS_2026_08_13_SURFACE_FUSION = """
/api/surface/capabilities	GET
/api/surface/pilot	GET,POST
"""
_BASELINE += _ADDITIONS_2026_08_13_SURFACE_FUSION

# 2026-08-13 驾驶舱 P0（takeover_routes）：会话级一键接管/交还——档位切 manual
# （AI 全停）+ 在途草稿取消 + 会话标签，打包成一个坐席动作；active/status 供
# 会话头按钮与超时提醒消费。
_ADDITIONS_2026_08_13_TAKEOVER = """
/api/takeover/active	GET
/api/takeover/status	GET
/api/takeover/start	POST
/api/takeover/end	POST
"""
_BASELINE += _ADDITIONS_2026_08_13_TAKEOVER

# 2026-08-13 驾驶舱 P1（cockpit）：介入优先级队列页 + 聚合 API（四源：接管超时/
# 需人工/客户在等/草稿待审，去重排序 30s 缓存；KPI 与账号健康复用既有端点不重造）。
_ADDITIONS_2026_08_13_COCKPIT = """
/workspace/cockpit	GET
/api/cockpit/overview	GET
"""
_BASELINE += _ADDITIONS_2026_08_13_COCKPIT

# 2026-08-14 驾驶舱 P2（resolve）：「已处理」摘「需人工」标签的唯一清除出口
# （标签无自动过期语义，上线首日实测 12～34 天陈尸卡占屏）。overview 同批加
# caps 特性探测位（模板热更先于重启的中间态，前端见不到 caps 不渲染按钮）。
_ADDITIONS_2026_08_14_COCKPIT_RESOLVE = """
/api/cockpit/resolve	POST
"""
_BASELINE += _ADDITIONS_2026_08_14_COCKPIT_RESOLVE

# 2026-08-16 会话删除（boss 直接需求）：工作台删除单会话全部本地数据（全表硬删
# + 防复活墓碑，真实新消息自动解除回显；拒 agent/viewer）。账号删除的连带清库
# 走既有 /api/accounts/*/remove 的 body 扩展（purge_data），不新增端点。
_ADDITIONS_2026_08_16_CONV_DELETE = """
/api/unified-inbox/conversations/delete	POST
"""
_BASELINE += _ADDITIONS_2026_08_16_CONV_DELETE

# 2026-08-17 官方级消息管理（boss「与官方一致的会话/消息管理」）：会话置顶（服务端
# 落库，全坐席共见）+ 清空聊天记录（仅工作台，supervisor+）+ 消息「仅工作台删除」
# 软删/恢复（undo 服务端退路）+ 能力探测 meta（feat 特性探测：旧后端 404 → 前端
# 新 UI 整体不挂；平台撤回能力单一事实源）。双端撤回复用既有
# /api/platforms/{platform}/{account_id}/message-op（已扩 telegram/line），不新增端点。
_ADDITIONS_2026_08_17_MSG_OPS = """
/api/unified-inbox/conversations/pin	POST
/api/unified-inbox/conversations/clear	POST
/api/unified-inbox/messages/delete	POST
/api/unified-inbox/messages/restore	POST
/api/unified-inbox/message-ops/meta	GET
"""
_BASELINE += _ADDITIONS_2026_08_17_MSG_OPS

# 2026-08-17 消息管理 P2（审计可视化 + 回收站）：软删消息列表（supervisor+，配
# restore 端点=误删/误清空的主管级找回）+ 删除/撤回审计读数（ops_events 台账 →
# ops-overview「消息管理审计」卡：计数/撤回成功率/失败归因/最近操作流）。
_ADDITIONS_2026_08_17_MSG_OPS_P2 = """
/api/unified-inbox/messages/deleted	GET
/api/admin/msg-ops-stats	GET
"""
_BASELINE += _ADDITIONS_2026_08_17_MSG_OPS_P2

# 2026-08-31 新手引导退役：WP-2 首启向导（/welcome + /api/onboarding/*）与
# WP-7 坐席新手任务（/api/agent-tasks*）路由不再注册——条目已从清单移除，
# 退役防复活门禁见 tests/test_onboarding_retired.py。

# 2026-08-17 WP-4 合规只读导出（compliance_routes）：危机转介计数（SB 243 年报
# 数字）+ 合规开关回显。写入面在危机处置链打点（record_crisis_referral），
# 本端点只读零副作用。
_ADDITIONS_2026_08_17_COMPLIANCE = """
/api/admin/crisis-referrals	GET
"""
_BASELINE += _ADDITIONS_2026_08_17_COMPLIANCE

# 2026-08-17 WP-3 老板日报（boss_routes）：/workspace/boss 页（任意登录角色，
# 零工程黑话的钱/时间视角）+ 日账/周账/趋势/省时聚合（300s TTL）+ 周报 Markdown
# 导出（WS-3 案例采写素材）。口径=value_report 持久库（日窗=滚动 24h）。
_ADDITIONS_2026_08_17_BOSS = """
/workspace/boss	GET
/api/workspace/boss-value	GET
/api/workspace/boss-export.md	GET
"""
_BASELINE += _ADDITIONS_2026_08_17_BOSS

# 2026-08-17 账号真相闭环 P1/P2：软删账号恢复（removed→offline 可重登，与 remove
# 同权限；此前误删只能改库）+ 按账号批量清未读（历史/已退出号存量未读清账，
# 与逐会话 mark-read 同一 last_read_ts 水位机制，永不回弹）。
_ADDITIONS_2026_08_17_ACCT_TRUTH = """
/api/accounts/{platform}/{account_id}/restore	POST
/api/unified-inbox/mark-account-read	POST
"""
_BASELINE += _ADDITIONS_2026_08_17_ACCT_TRUTH

# 2026-08-17 历史账号治理 P0：彻底删除历史账号（store 级会话数据全清 +
# 注册表残行硬删；仅 offline/removed/history_only 可删，活跃号/config 常驻号/
# 内置合成号 409）。抽屉「历史 / 已退出账号」分区 ⋯ 菜单消费；与 /remove
# （软删在册号）语义互补——remove+purge 之后留下的「已移除 · 0 会话」死行
# 由本路由收尾。
_ADDITIONS_2026_08_17_ACCT_PURGE = """
/api/accounts/{platform}/{account_id}/purge-history	POST
"""
_BASELINE += _ADDITIONS_2026_08_17_ACCT_PURGE

# 2026-08-17 历史账号治理 P1：删除前体量预估（GET 同路径零副作用——会话/消息数
# + purgeable 判定，与 POST 共用 _purge_history_blocked 单点）+ JSONL 导出
# （资产保全「先备份再删」；任意账号可导=备份语义，manager 权限 + account_export 审计）。
_ADDITIONS_2026_08_17_ACCT_PURGE_P1 = """
/api/accounts/{platform}/{account_id}/purge-history	GET
/api/accounts/{platform}/{account_id}/export-history	GET
"""
_BASELINE += _ADDITIONS_2026_08_17_ACCT_PURGE_P1

# 2026-08-17 表情包（贴纸）主线（sticker_routes）：包/条目管理 + 收藏入站贴纸 +
# 官方包播种 + 跨平台发送（TG/WA 原生贴纸、LINE 官方商店贴纸原生、其余图片回退，
# 响应带 sent_as）。feature flag inbox.stickers.enabled 默认关。
_ADDITIONS_2026_08_17_STICKERS = """
/api/stickers/status	GET
/api/stickers/packs	GET
/api/stickers/packs	POST
/api/stickers/packs/{pack_id}	PATCH
/api/stickers/packs/{pack_id}	DELETE
/api/stickers/packs/{pack_id}/items	GET
/api/stickers/packs/{pack_id}/items	POST
/api/stickers/packs/{pack_id}/items/{sid}	DELETE
/api/stickers/collect	POST
/api/stickers/seed-official	POST
/api/unified-inbox/send-sticker	POST
"""
_BASELINE += _ADDITIONS_2026_08_17_STICKERS

# 2026-08-18 跨平台档案（cp-origin 面板）：客户来源/原平台背景 GET+表单写入 POST
# + P1 聊天记录导入三件套（解析预览/确认写入/整批撤销）。
# （unified_inbox_workspace_contacts_routes.py；flag contacts.origin_profile.enabled）。
_ADDITIONS_2026_08_18_ORIGIN = """
/api/workspace/origin	GET
/api/workspace/origin	POST
/api/workspace/origin/import/parse	POST
/api/workspace/origin/import/confirm	POST
/api/workspace/origin/import/revoke	POST
"""
_BASELINE += _ADDITIONS_2026_08_18_ORIGIN

# 2026-08-19 账号资产中心（asset_center_routes.py，账号资产保全 P1）：资产总览页 +
# 汇总/台账只读 API。卡片 CTA 的 export-migration / reconnect 端点按路由表
# feature-probe，未装载不渲染——那些端点由接管线 / 回连认领线各自入册。
_ADDITIONS_2026_08_19_ASSET_CENTER = """
/workspace/assets	GET
/api/workspace/assets/summary	GET
/api/workspace/assets/ledger	GET
"""
_BASELINE += _ADDITIONS_2026_08_19_ASSET_CENTER

# 2026-08-20 客服支持通道（support_routes.py，实施49 P1-9）：机器码/版本自查 +
# 坐席可用的一键诊断直传。刻意不复用 /api/admin/diagnostic-upload——ROLE_AGENT
# 被 _agent_api_allowed 挡在 admin 命名空间外，而报障主力恰是坐席。
_ADDITIONS_2026_08_20_SUPPORT = """
/api/support/info	GET
/api/support/diag-upload	POST
"""
_BASELINE += _ADDITIONS_2026_08_20_SUPPORT

# 2026-08-21 quotawall v2 P2.5：额度墙漏斗卡「服务端到账真值」对账线——
# qw_credited 埋点只统计页面开着等到 watch 命中的场景（关页即漏计），此端点读
# license_char_topup 台账（凭证兑换/手工直充/自动履约同一张表）按日聚合，
# 响应只含 day/n/chars 数字（无 lic_id 无订单号）。
_ADDITIONS_2026_08_21_TOPUP_TREND = """
/api/admin/license/topup-trend	GET
"""
_BASELINE += _ADDITIONS_2026_08_21_TOPUP_TREND

# 2026-08-23 标签治理闭环（unified_inbox_workspace_tags_routes.py）：把某标签从
# **所有**会话上摘除（含已归档）。库删除刻意不动已打标签（既有语义），此前
# 「123/333」类测试标签永远挂在筛选条上没有任何全量摘除入口；viewer 拒写，
# ops_events 留审计（tag_remove_all）。
_ADDITIONS_2026_08_23_TAG_REMOVE_ALL = """
/api/workspace/tags/remove-from-all	POST
"""
_BASELINE += _ADDITIONS_2026_08_23_TAG_REMOVE_ALL

# 2026-08-26 B88 群线程缺口补拉（实施68 P1-13）：打开会话时对比云端顶部 id 与
# 镜像最大 id，有缺口自动 sync（/thread 内触发）；本端点＝前端「补拉失败」横幅的
# 手动重试（豁免 120s 冷却，running 单飞仍生效）。见 unified_inbox_account_routes。
_ADDITIONS_2026_08_26_GAP_PROBE = """
/api/platforms/telegram/{account_id}/gap-probe	POST
"""
_BASELINE += _ADDITIONS_2026_08_26_GAP_PROBE

# 2026-08-27 实施72 登录身份决议（账号错乱事故根治）：登录位换人 → 新账号进
# 「身份待确认」隔离态（不自动补挂人设 + 自动化封顶 review + 告警审计）。
# pending＝隔离清单观测面（任意登录可读）；confirm＝人工转正写口（拒 agent/viewer，
# 可显式指定人设）。见 src/integrations/account_identity.py。
_ADDITIONS_2026_08_27_ACCOUNT_IDENTITY = """
/api/admin/account-identity/pending	GET
/api/admin/account-identity/confirm	POST
"""
_BASELINE += _ADDITIONS_2026_08_27_ACCOUNT_IDENTITY

# 2026-08-28 迁移包导出（实施47 §5 工单 2，migration_export_routes.py）：把某账号的
# 联系人（人的并集 + conversations 身份列 + CRM best-effort）、会话/消息、可选媒体
# 打成一份离线 zip，manifest 带 sha256 与体量对账。资产中心页自 08-19 起就在按
# 路径探测 /export-migration（features.export_migration），端点缺席时 CTA 不渲染 →
# 装载即点亮，前端零改动。preview 是零副作用体量/覆盖率预览，与真导出同一套取数。
_ADDITIONS_2026_08_28_MIGRATION_EXPORT = """
/api/accounts/{platform}/{account_id}/export-migration	GET
/api/accounts/{platform}/{account_id}/migration-preview	GET
"""
_BASELINE += _ADDITIONS_2026_08_28_MIGRATION_EXPORT

# 2026-08-28 实施81 报障工单处置台：/admin/bug-tickets
# ＝实施74 §6.1 推迟的「客服工单处置页」（列表/详情/截图/状态流转/一键回访）；
# /reply＝处置台「群内回复」——@报障人 + 工单号 footer 经工单归属账号发进原群，
# 成功自动回写工单 note（值守回复从手写一次性脚本收口成产品动作）。
_ADDITIONS_2026_08_28_BUG_TICKETS = """
/admin/bug-tickets	GET
/api/admin/bug-intake/{ticket_id}/reply	POST
"""
_BASELINE += _ADDITIONS_2026_08_28_BUG_TICKETS

# 2026-08-29 代登记（归属：主动关怀线）：/api/care/outreach-timeline 已随 HEAD
# 提交在应用注册，但清单漏登 → extra-route 门禁红。按「已提交的主动新增须登记」
# 契约代为补行；若关怀线另有登记块，合并时以其为准删本行。
_ADDITIONS_2026_08_29_CARE_TIMELINE = """
/api/care/outreach-timeline	GET
"""
_BASELINE += _ADDITIONS_2026_08_29_CARE_TIMELINE

# 2026-08-29 代登记（归属：小智助手线，faq 端点已随其今晨提交上线并实测 200——
# 见意向板「api/assistant/faq 200 with real qa_log data」）：清单漏登 → extra-route
# 门禁红挡全树 preflight。按「已提交的主动新增须登记」契约代为补行；若助手线
# 另有登记块，合并时以其为准删本行。
_ADDITIONS_2026_08_29_ASSISTANT_FAQ = """
/api/assistant/faq	GET
"""
_BASELINE += _ADDITIONS_2026_08_29_ASSISTANT_FAQ

# 2026-08-29 设置页「账号发送额度」今日用量（与 budget-today 同页正交）
_ADDITIONS_2026_08_29_SENDGATE_TODAY = """
/api/reply-settings/sendgate-today	GET
"""
_BASELINE += _ADDITIONS_2026_08_29_SENDGATE_TODAY

# 2026-08-30 新账号「AI 接管方式」确认（全自动/拟稿人审/关闭，默认拟稿人审）：
# 待确认/已确认清单 + 决策写入（主管；account_mode_routes.py）
_ADDITIONS_2026_08_30_ACCOUNT_MODES = """
/api/reply-settings/account-modes	GET
/api/reply-settings/account-modes/decide	POST
"""
_BASELINE += _ADDITIONS_2026_08_30_ACCOUNT_MODES

# 2026-08-29 一键预设只读预览（运营关闸时套用前能看见不会重开闸）
_ADDITIONS_2026_08_29_PRESET_PREVIEW = """
/api/companion/capabilities/preset-preview	GET
"""
_BASELINE += _ADDITIONS_2026_08_29_PRESET_PREVIEW

# 2026-08-31 #113 临时行程状态治理：一键清除（写 conversation_meta.
# travel_cleared_ts 水位，草稿链/send-caps 可见面按水位忽略更早的行程自述，
# 叙事立即回归档案常驻地）。会话头行程徽标 ✕ 按钮消费。
_ADDITIONS_2026_08_31_TRAVEL_STATE = """
/api/unified-inbox/travel-state/clear	POST
"""
_BASELINE += _ADDITIONS_2026_08_31_TRAVEL_STATE

# 2026-09-01 #132 短链公网基址界面化：SOP 页可直接配置 ``inbox.cta.public_base``
# overlay（unified_inbox_workflow_routes，随 b7161370 提交但漏登基线——
# 2026-09-02 preflight 装配门禁抓到后补录）。
_ADDITIONS_2026_09_01_CTA_PUBLIC_BASE = """
/api/workspace/cta-public-base	POST
"""
_BASELINE += _ADDITIONS_2026_09_01_CTA_PUBLIC_BASE

# 2026-09-02 #142 真发总闸三件套（companion_capability_routes，随 ae772df1
# 提交但漏登基线）：主管一键恢复真发（worker/deliver 被关过的一并打开，
# _require_supervisor 同闸，翻动经 _audit_toggle 留痕）。
_ADDITIONS_2026_09_02_DELIVER_GATE_RESUME = """
/api/companion/deliver-gate/resume	POST
"""
_BASELINE += _ADDITIONS_2026_09_02_DELIVER_GATE_RESUME

# 2026-09-02 C2 值守可见性：bug_events 台账只读口——被限频静默的真反馈
# （rate_capped_report）/使用咨询（usage）不产生 bot 回执与工单，此前只躺在
# 台账里；duty_watchdog 据此纳入「未应答告警」扫描面（限流只限 bot 自动
# 回执，不得限值守可见性）。
_ADDITIONS_2026_09_02_BUG_INTAKE_EVENTS = """
/api/admin/bug-intake/events	GET
"""
_BASELINE += _ADDITIONS_2026_09_02_BUG_INTAKE_EVENTS

# 2026-09-03 #159 账号栏幽灵未读：徽标那个数字**具体是哪几条**（与徽标聚合
# 共用同一份 WHERE，点进去的条数与数字恒等）。坐席据此直达那 N 条，或走既有
# mark-account-read 一键清零。
_ADDITIONS_2026_09_03_ACCOUNT_UNREAD = """
/api/unified-inbox/account-unread	GET
"""
_BASELINE += _ADDITIONS_2026_09_03_ACCOUNT_UNREAD

# 2026-09-03 #155 人设归属层级：双向称呼（你称呼对方 / 对方称呼你）从人设档
# 迁到**联系人级**——同一人设服务一百个客户，爱称是客户关系属性不是人设属性。
# 读口附带人设级值供 UI 渲染「未设置时沿用人设的 xxx」。
_ADDITIONS_2026_09_03_ADDRESS_NAMES = """
/api/unified-inbox/conv-meta/address-names	GET
/api/unified-inbox/conv-meta/address-names	POST
"""
_BASELINE += _ADDITIONS_2026_09_03_ADDRESS_NAMES

# 2026-09-05 J-8 #182 手动加关怀「意图反转」修复：intent-check = 前端逐键的
# 「这像是要发的话还是客户的事」判定（与 schedule POST 的服务端闸门同一纯函数）；
# send-text = 预览上手改终稿直送出站队列（零 LLM，运营对文本负责）。
_ADDITIONS_2026_09_05_CARE_VERBATIM = """
/api/care/intent-check	POST
/api/care/schedule/{sid}/send-text	POST
"""
_BASELINE += _ADDITIONS_2026_09_05_CARE_VERBATIM

# 2026-09-06 M-4 A #222 幽灵未读第三轮：被埋（归档着却有未读）会话一键标已读——
# 横幅第二动作，与横幅 / 主徽标同一份 WHERE（unread_aggregate.buried_conversations），
# 装载清扫只清「人工归档 + 无入站 > 72h」的存量，<72h 的由坐席在此定夺。
_ADDITIONS_2026_09_06_BURIED_MARK_READ = """
/api/unified-inbox/buried-mark-read	POST
"""
_BASELINE += _ADDITIONS_2026_09_06_BURIED_MARK_READ

# 2026-09-06 M-2 B（D-M1 登录默认半自动）：账号级批量切档 + 账号门禁（冷静期 / 降级 /
# 退避 / 未连接）读写——unified_inbox_stored_read_routes.py L567 / L611。
# M-2 收工未登记，M-6 收口 preflight 抓出后代登记（清单即门禁台账，非业务代码）。
_ADDITIONS_2026_09_06_M2_ACCOUNT_GATE = """
/api/unified-inbox/automation/account-gate	POST
/api/unified-inbox/automation/account-bulk	POST
"""
_BASELINE += _ADDITIONS_2026_09_06_M2_ACCOUNT_GATE

# 2026-09-07 N-3（#241 #240 域包错配与预置清理）：A 业务域单一真值读写（开发者页下拉，
# `01837c2b`）/ B 陪伴域「+ 自定义标签」读写（`4331c851`）/ E 系统预置支付话术一键清除
# （`d46b62a9`）。N-3 收工未登记，N-5 收口 preflight 抓出后代登记（清单即门禁台账，非业务代码）。
_ADDITIONS_2026_09_07_N3_BUSINESS_DOMAIN = """
/api/developer/business-domain	GET
/api/developer/business-domain	POST
/api/goals/custom-slots	GET
/api/goals/custom-slots	POST
/api/kb/entries/purge-payment-seeds	POST
"""
_BASELINE += _ADDITIONS_2026_09_07_N3_BUSINESS_DOMAIN

# 实施97 微信 PC 副驾线 B 第三轮：桥接驱动进程存活心跳（`unified_inbox_desktop_routes.py`
# `api_desktop_heartbeat` + `desktop_bridge_presence.py`）——**代登记**（2026-09-08 05:5x，N-5 收口：
# 该线路由已落盘、清单行未及登记，红了全树装配门禁并挡住 1.0.77 发版重启；同 08-30 小智线三端点
# 先例。语义归属仍是实施97 线，端点行为以其实现为准；若该线改名 / 撤掉，请一并改这行）。
_ADDITIONS_2026_09_08_WECHAT_PC_HEARTBEAT = """
/api/desktop/heartbeat	POST
"""
_BASELINE += _ADDITIONS_2026_09_08_WECHAT_PC_HEARTBEAT

# 实施96 DY（2026-09-08 老板拍板「在页面做出教程和链接」）：渠道接入教程页——抖音企业版
# （企业主体小程序 + 能力实验室）/ TikTok 官方通道 / 付款方式；session auth，按 ui_lang 中英，
# 数据与小智问答同源（src/assistant/onboarding_guides.py）。`onboarding_guide_routes.py`。
_ADDITIONS_2026_09_08_DY_ONBOARDING_GUIDE = """
/help/onboarding/{slug}	GET
/workspace/onboarding/{slug}	GET
/workspace/onboarding/douyin/credentials	POST
/workspace/onboarding/douyin/authorize	GET
/webhook/douyin/oauth/callback	GET
/workspace/onboarding/tiktok/account	POST
/workspace/onboarding/tiktok/credentials	POST
/workspace/onboarding/tiktok/authorize	GET
/webhook/tiktok/oauth/callback	GET
/workspace/onboarding/tiktok/webhook	POST
/api/onboarding/{slug}/status	GET
/workspace/onboarding/{slug}/step/{n}	POST
"""
_BASELINE += _ADDITIONS_2026_09_08_DY_ONBOARDING_GUIDE

# 实施97 微信线第四/六轮（2026-09-08）：微信客服会话状态动作（转企微人工 / 结束 / 查状态，
# `wechat_kf_webhook.register_wechat_kf_session_routes`）+ 五步接入引导后端（`wechat_kf_setup_routes.py`：
# 出站 IP / 凭证测试 / 客服账号列表与新建 / 绑定拉起 / 客户二维码 / AI 接待）+ 个人微信 PC 副驾引导后端
# （`wechat_pc_setup_routes.py`：环境检测 / 档位与知情同意 / 启动命令）+ 引导页 `/workspace/connect/{platform}`。
_ADDITIONS_2026_09_08_WECHAT_CONNECT_GUIDE = """
/api/unified-inbox/kf/session-state	GET
/api/unified-inbox/kf/transfer	POST
/api/unified-inbox/kf/close	POST
/api/setup/wechat_kf/egress-ip	GET
/api/setup/wechat_kf/test	POST
/api/setup/wechat_kf/accounts	GET,POST
/api/setup/wechat_kf/bind	POST
/api/setup/wechat_kf/contact-way	POST
/api/setup/wechat_kf/reception	POST
/api/setup/wechat_pc/env	GET
/api/setup/wechat_pc/policy	GET,POST
/api/setup/wechat_pc/start-command	GET
/api/setup/wechat_pc/prepare	POST
/workspace/connect/{platform}	GET
/login/wecom	GET
/login/wecom/callback	GET
/api/auth/wecom/status	GET
"""
_BASELINE += _ADDITIONS_2026_09_08_WECHAT_CONNECT_GUIDE

# QQ 双轨·个人号线（2026-09-08）：协议登录风险知情同意 + 协议端使用协议页——**代登记**（实施97 微信线
# 在跑全树门禁时发现已落盘未登记，同 WECHAT_PC_HEARTBEAT 先例；语义归属 QQ 线，改名/撤掉请一并改这两行）。
_ADDITIONS_2026_09_08_QQ_PERSONAL_CONSENT = """
/api/platforms/qq/risk-consent	POST
/help/qq-personal-agreement	GET
/api/platforms/qq/download-qq	POST
/api/platforms/qq/qq-status	GET
"""
_BASELINE += _ADDITIONS_2026_09_08_QQ_PERSONAL_CONSENT

# P-4 #254（D-P6，2026-09-08）：陪伴域首装 KB 为空——「新建条目」预填模板（按域）+
# 「清除客服域残留」两步走（默认 dry_run 只列清单；显式 dry_run=false + ids 才删）。
_ADDITIONS_2026_09_08_P4_KB_FACTORY_STATE = """
/api/kb/new-entry-templates	GET
/api/kb/entries/purge-legacy-seeds	POST
"""
_BASELINE += _ADDITIONS_2026_09_08_P4_KB_FACTORY_STATE

# P-2 #259 #252（D-P1，2026-09-08）：「沉寂会话待你决定」清单快照 + 三按钮（ignore / manual /
# draft 永远 review）+ 登录确认框 ack——unified_inbox_stored_read_routes.py。
_ADDITIONS_2026_09_08_P2_DORMANT_REVIEW = """
/api/unified-inbox/dormant-review	GET
/api/unified-inbox/dormant-review/action	POST
"""
_BASELINE += _ADDITIONS_2026_09_08_P2_DORMANT_REVIEW

# Q-14 #262（2026-09-09）：「AI 本轮未生成」灰标读 / 清（ai_fail_routes.py）。灰标由起草侧写、
# 下次 AI 成功自动清；clear 给坐席「重试起草」成功后手动清。
_ADDITIONS_2026_09_09_Q14_AI_FAIL = """
/api/unified-inbox/ai-last-fail	GET
/api/unified-inbox/ai-last-fail/all	GET
/api/unified-inbox/ai-last-fail/clear	POST
"""
_BASELINE += _ADDITIONS_2026_09_09_Q14_AI_FAIL


def _parse_baseline():
    expected = set()
    for line in _BASELINE.strip().splitlines():
        if "\t" not in line:
            continue
        path, methods = line.split("\t", 1)
        for m in methods.split(","):
            m = m.strip()
            if m:
                expected.add((path.strip(), m))
    return expected


EXPECTED_ROUTES = _parse_baseline()


def _live_routes(app):
    live = set()
    for r in app.routes:
        path = getattr(r, "path", None)
        if not path:
            continue
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            live.add((path, m))
    return live


def test_baseline_parsed_nontrivial():
    # 防止基线被误清空
    assert len(EXPECTED_ROUTES) >= 450


def test_all_baseline_routes_still_registered(app):
    live = _live_routes(app)
    missing = sorted(EXPECTED_ROUTES - live)
    assert not missing, (
        f"重构丢失了 {len(missing)} 个端点（admin.py 拆分安全网）：\n"
        + "\n".join(f"  {p} [{m}]" for p, m in missing)
    )


def test_no_unexpected_extra_routes(app):
    """精确相等：除基线外不应出现新端点（拆分期间应只搬迁、不新增/改名）。"""
    live = _live_routes(app)
    extra = sorted(live - EXPECTED_ROUTES)
    assert not extra, (
        f"出现 {len(extra)} 个基线外端点（拆分应只搬迁；如确为有意新增，请更新基线）：\n"
        + "\n".join(f"  {p} [{m}]" for p, m in extra)
    )


# 已知的历史重复注册（pre-existing，非拆分引入）。Starlette 用首个匹配，
# 后注册的被遮蔽。列入白名单，使本测试能抓出「新增」的重复注册。
_KNOWN_DUPLICATE_ROUTES = {
    # /api/kb/import 同 path 注册两次但语义不同：kb_routes(export-dump 导入，生效) 与
    # admin.py inline(KBImporter 文档导入，被遮蔽)。属待产品决策的遗留 bug（需改名才能并存），
    # 暂保留白名单。详见 admin.py「KB Import API」处注释。
    ("/api/kb/import", "POST"),
    # 注：persona 6 端点的 inline 重复已在 Phase E1 清理（删 inline 死代码，
    # 统一由 register_persona_routes 提供），不再是重复注册。
}


def test_no_new_duplicate_route_registrations(app):
    """检测重复注册的 (path, method)。已知历史重复白名单豁免，新增重复则失败。"""
    from collections import Counter

    counts = Counter()
    for r in app.routes:
        path = getattr(r, "path", None)
        if not path:
            continue
        for m in (getattr(r, "methods", None) or set()):
            if m in {"HEAD", "OPTIONS"}:
                continue
            counts[(path, m)] += 1
    dups = {k for k, c in counts.items() if c > 1}
    new_dups = sorted(dups - _KNOWN_DUPLICATE_ROUTES)
    assert not new_dups, (
        f"出现 {len(new_dups)} 个新的重复注册端点：\n"
        + "\n".join(f"  {p} [{m}]" for p, m in new_dups)
    )
