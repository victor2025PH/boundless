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
/api/admin/ui-event-trend	GET
/api/admin/identity-health-trend	GET
/api/admin/anti-repeat-advice	GET
/api/admin/ai-safety-overview	GET
/api/admin/ui-event	POST
/api/telemetry/frontend-error	POST
/api/admin/ai-quality-calibrate	GET
/api/admin/ai-quality-thresholds	POST
/api/admin/instance-restart-status	GET
/api/admin/platform-sessions/relogin	POST
/api/admin/profile-audit	GET
/api/admin/realtime-voice-alert-calibrate	GET
/api/admin/realtime-voice-alert-thresholds	POST
/api/admin/realtime-voice-trend	GET
/api/admin/gpu-watermark	GET
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
/api/care/schedule/{sid}/preview	POST
/api/care/dry-run-samples	GET
/api/care/dry-run-feedback	POST
/api/care/health	GET
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
/api/crisis-events/{event_id}/handle	POST
/api/data-purge	POST
/api/episodic-memory	GET
/api/episodic-memory/backfill	POST
/api/episodic-memory/correction-stats	GET
/api/episodic-memory/{row_id}	DELETE
/api/episodic-memory/{row_id}/confirm	POST
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
/api/personas/profiles/{profile_id}/diff-canonical	GET
/api/personas/profiles/{profile_id}/history	GET
/api/personas/profiles/{profile_id}/promote	POST
/api/personas/profiles/{profile_id}/prompt-preview	GET
/api/personas/profiles/{profile_id}/revert	POST
/api/personas/status	GET
/api/personas/sync-to-config	POST
/api/personas/tg-account/{account_id}/assign-profile	POST
/api/personas/wa-account/{account_id}/assign-profile	POST
/api/personas/{pid}/media	GET
/api/personas/{pid}/media	POST
/api/personas/{pid}/media/test	POST
/api/personas/{pid}/media/{mid}	DELETE
/api/personas/{pid}/media/{mid}	PATCH
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
/api/platforms/{platform}/login/{login_id}/cancel	POST
/api/proxies	GET
/api/proxies	POST
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
/api/platforms/{platform}/{account_id}/react	POST
/api/platforms/{platform}/{account_id}/message-op	POST
/api/platforms/{platform}/{account_id}/group-members	GET
/api/unified-inbox/send-media	POST
/api/unified-inbox/send-voice	POST
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
/api/desktop/outbound	GET
/api/desktop/outbound/ack	POST
/api/desktop/outbound/action	POST
/api/desktop/outbound/corrections	GET
/api/desktop/outbound/rewrite	POST
/api/desktop/outbound/stats	GET
/api/desktop/fingerprint	GET
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
/api/trigger-decisions	GET
/api/unified-inbox/analyze	POST
/api/unified-inbox/automation	GET
/api/unified-inbox/automation	POST
/api/unified-inbox/automation/bulk-downgrade	POST
/api/unified-inbox/automation-stats	GET
/api/unified-inbox/chats	GET
/api/unified-inbox/history	GET
/api/unified-inbox/kb-search	GET
/api/unified-inbox/mark-conversion	POST
/api/unified-inbox/search-messages	GET
/api/unified-inbox/profile	GET
/api/unified-inbox/templates	GET
/api/unified-inbox/send	POST
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
/api/unified-inbox/translate	POST
/api/unified-inbox/translate-compare	POST
/api/unified-inbox/translate-document	POST
/api/unified-inbox/translate-document-file	POST
/api/unified-inbox/translate-document-progress/{job_id}	GET
/api/unified-inbox/translated-file/{token}	GET
/api/unified-inbox/translate-image	POST
/api/unified-inbox/translate-message-media	POST
/api/unified-inbox/translate-voice	POST
/api/unified-inbox/translation-engines	GET
/api/workspace/claim	POST
/api/workspace/claim/release	POST
/api/workspace/claim/renew	POST
/api/workspace/claims	GET
/api/workspace/glossary	GET
/api/workspace/glossary	POST
/api/workspace/typing	POST
/api/workspace/contact/{contact_id}	GET
/api/workspace/contact/{contact_id}/crm	POST
/api/workspace/contact/{contact_id}/follow-up	POST
/api/workspace/contact/{contact_id}/tasks	GET
/api/workspace/contact/{contact_id}/timeline	GET
/api/workspace/conv/{conversation_id}/next-actions	GET
/api/workspace/conv/{conversation_id}/execute-action	POST
/api/workspace/conv/{conversation_id}/start-chain	POST
/api/workspace/workflow-actions	GET
/api/workspace/workflow-actions	POST
/api/workspace/workflow-actions/{action_id}	PUT
/api/workspace/workflow-actions/{action_id}	DELETE
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
/api/setup/cloud-credentials	GET
/api/setup/key-pool	POST
/api/setup/features	GET
/api/setup/features/toggle	POST
/api/workspace/ai-runtime-status	GET
/api/workspace/hosted-quota	GET
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
/personas	GET
/rpa-overview	GET
/set_lang	GET
/set_ui_mode	GET
/settings	GET
/setup	GET
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

# 2026-07-27 人设上线前质检（E 线统一登记两家）：quiz=档案自动出题→真人设 prompt
# 逐题实测→自动判分（persona_quiz_routes.py，任务经 persona_doc_import 注册表轮询）；
# bio-doc=D2 线「人设传记文档」三端点（契约固定先行入册，路由实现由 D2 落地）。
_ADDITIONS_2026_07_27_PERSONA_QUIZ_BIO = """
/api/personas/quiz/status	GET
/api/personas/{profile_id}/quiz	POST
/api/personas/{profile_id}/quiz/jobs/{job_id}	GET
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
