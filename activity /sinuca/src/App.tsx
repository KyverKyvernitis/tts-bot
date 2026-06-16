import { useEffect, useMemo, useState } from "react";
import {
  authenticateDiscordAccessToken,
  authorizeDiscordCode,
  bootstrapDiscord,
  clearCachedToken,
  getDiscordSdk,
  getOAuthRedirectUri,
  readCachedToken,
  writeCachedToken,
  writeCachedUser,
} from "./sdk/discord";
import type { ActivityBootstrap } from "./types/activity";
import type { DashboardFieldDefinition, DashboardSectionDefinition, DashboardSectionSummary } from "./types/dashboard";
import { exchangeDiscordTokenRequest } from "./transport/sessionApi";
import {
  fetchDashboardBootstrap,
  fetchDashboardSettings,
  fetchDashboardSummary,
  patchDashboardSettings,
} from "./transport/dashboardApi";

const pendingBootstrap: ActivityBootstrap = {
  sdkReady: false,
  clientId: null,
  context: { mode: "casual", instanceId: null, guildId: null, channelId: null, source: "fallback" },
  currentUser: { userId: "pending-auth", displayName: "Carregando usuário...", avatarUrl: null },
  bootDebug: [],
};

function isSnowflake(value: string | null | undefined): value is string {
  return typeof value === "string" && /^\d{15,25}$/.test(value);
}

function stringifyValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return Number.isFinite(value) && value > 0 ? String(value) : "";
  return String(value);
}

function displayValue(field: DashboardFieldDefinition, value: unknown): string {
  if (field.type === "boolean") return value ? "Ligado" : "Desligado";
  if ((field.type === "channel" || field.type === "role") && Number(value || 0) > 0) {
    return field.type === "channel" ? `<#${value}>` : `<@&${value}>`;
  }
  if (field.type === "select") {
    const raw = stringifyValue(value);
    return field.options?.find((item) => item.value === raw)?.label ?? raw;
  }
  const text = stringifyValue(value).trim();
  return text || "Não configurado";
}

function normalizeInputValue(field: DashboardFieldDefinition, raw: string | boolean): unknown {
  if (field.type === "boolean") return Boolean(raw);
  if (field.type === "number") return Number(raw || 0);
  if (field.type === "channel" || field.type === "role") {
    const match = String(raw || "").match(/\d{15,25}/);
    return match ? match[0] : "";
  }
  return raw;
}

function sectionConfiguredLabel(section: DashboardSectionSummary | undefined) {
  if (!section) return "0/0";
  const enabled = section.enabled === null ? "" : section.enabled ? " · ativo" : " · off";
  return `${section.configured}/${section.total}${enabled}`;
}

function cleanErrorText(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error || "");
  if (!raw.trim()) return "Não consegui abrir o dashboard.";
  if (raw.includes("<!doctype") || raw.includes("<html") || raw.includes("html_frontend")) {
    return "A API do dashboard ainda está respondendo com HTML do frontend. Atualize backend/proxy e tente reautenticar.";
  }
  if (raw.includes("api_proxy_returning_frontend_html")) {
    return "A autenticação caiu no frontend em vez do backend. O patch adiciona rotas /token e /api/token para corrigir isso.";
  }
  return raw.replace(/\s+/g, " ").slice(0, 260);
}

function statusClass(summary: DashboardSectionSummary | undefined) {
  if (!summary) return "neutral";
  if (summary.enabled === false) return "off";
  if (summary.configured <= 0) return "pending";
  if (summary.configured >= summary.total) return "ready";
  return "partial";
}

export default function App() {
  const [bootstrap, setBootstrap] = useState<ActivityBootstrap>(pendingBootstrap);
  const [token, setToken] = useState<string | null>(() => readCachedToken());
  const [authState, setAuthState] = useState<"booting" | "needs_login" | "ready" | "denied" | "error">("booting");
  const [message, setMessage] = useState<string>("Abrindo Dashboard...");
  const [sections, setSections] = useState<DashboardSectionDefinition[]>([]);
  const [summary, setSummary] = useState<DashboardSectionSummary[]>([]);
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [selectedSectionId, setSelectedSectionId] = useState<string>("general");
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [query, setQuery] = useState("");

  const guildId = bootstrap.context.guildId;
  const selectedSection = useMemo(
    () => sections.find((section) => section.id === selectedSectionId) ?? sections[0] ?? null,
    [sections, selectedSectionId],
  );
  const selectedSummary = useMemo(
    () => summary.find((item) => item.id === selectedSection?.id),
    [summary, selectedSection?.id],
  );

  const changedFields = useMemo(() => {
    if (!selectedSection) return [];
    return selectedSection.fields.filter((field) => draft[field.id] !== values[field.id]);
  }, [draft, selectedSection, values]);

  const filteredSections = useMemo(() => {
    const text = query.trim().toLowerCase();
    if (!text) return summary;
    return summary.filter((item) => {
      const section = sections.find((candidate) => candidate.id === item.id);
      const fieldHit = section?.fields.some((field) => `${field.label} ${field.description ?? ""}`.toLowerCase().includes(text));
      return `${item.label} ${item.description} ${item.status}`.toLowerCase().includes(text) || Boolean(fieldHit);
    });
  }, [query, sections, summary]);

  const dashboardStats = useMemo(() => {
    const totalFields = summary.reduce((acc, item) => acc + item.total, 0);
    const configured = summary.reduce((acc, item) => acc + item.configured, 0);
    const active = summary.filter((item) => item.enabled === true).length;
    const pending = Math.max(0, totalFields - configured);
    const percent = totalFields > 0 ? Math.round((configured / totalFields) * 100) : 0;
    return { totalFields, configured, active, pending, percent };
  }, [summary]);

  async function login(prompt: "none" | "consent" = "consent") {
    const discord = getDiscordSdk();
    if (!discord) {
      setAuthState("error");
      setMessage("SDK do Discord não está disponível nesta janela.");
      return;
    }
    setBusy(true);
    setMessage("Autorizando sua conta...");
    try {
      const auth = await authorizeDiscordCode(prompt);
      if (!auth.code) {
        setAuthState("needs_login");
        setMessage("Autorize sua conta para abrir o dashboard administrativo.");
        return;
      }
      const exchanged = await exchangeDiscordTokenRequest(auth.code, getOAuthRedirectUri());
      if (!exchanged.ok || !exchanged.accessToken) {
        setAuthState("error");
        setMessage(cleanErrorText(exchanged.error || exchanged.detail || "erro desconhecido"));
        return;
      }
      writeCachedToken(exchanged.accessToken);
      const user = await authenticateDiscordAccessToken(discord, exchanged.accessToken, guildId);
      if (user) writeCachedUser(user);
      setToken(exchanged.accessToken);
      setAuthState("ready");
      setMessage("Autorização concluída.");
    } catch (error) {
      setAuthState("error");
      setMessage(cleanErrorText(error));
    } finally {
      setBusy(false);
    }
  }

  async function loadDashboard(accessToken: string, targetGuildId: string) {
    setBusy(true);
    setMessage("Carregando configurações do servidor...");
    try {
      const [bootPayload, summaryPayload, settingsPayload] = await Promise.all([
        fetchDashboardBootstrap(accessToken, targetGuildId),
        fetchDashboardSummary(accessToken, targetGuildId),
        fetchDashboardSettings(accessToken, targetGuildId),
      ]);
      if (!bootPayload.ok || !summaryPayload.ok || !settingsPayload.ok) {
        throw new Error(bootPayload.error || summaryPayload.error || settingsPayload.error || "dashboard_load_failed");
      }
      setSections(settingsPayload.sections);
      setSummary(summaryPayload.sections);
      setValues(settingsPayload.values || {});
      setDraft(settingsPayload.values || {});
      if (!settingsPayload.sections.some((section) => section.id === selectedSectionId) && settingsPayload.sections[0]) {
        setSelectedSectionId(settingsPayload.sections[0].id);
      }
      setAuthState("ready");
      setMessage("Dashboard pronto.");
    } catch (error) {
      const text = cleanErrorText(error);
      if (text.includes("403") || text.includes("access_denied") || text.includes("missing_manage_guild")) {
        setAuthState("denied");
        setMessage("Você precisa ser dono, administrador ou ter Gerenciar servidor para configurar este servidor.");
      } else if (text.includes("401") || text.includes("missing_access_token") || text.includes("user_fetch_failed")) {
        clearCachedToken();
        setToken(null);
        setAuthState("needs_login");
        setMessage("Sessão expirada. Autorize novamente para continuar.");
      } else {
        setAuthState("error");
        setMessage(text || "Não consegui carregar o dashboard.");
      }
    } finally {
      setBusy(false);
    }
  }

  async function refreshDashboard() {
    if (!token || !guildId) return;
    await loadDashboard(token, guildId);
  }

  async function saveSection() {
    if (!token || !guildId || !selectedSection) return;
    const updates: Record<string, unknown> = {};
    for (const field of selectedSection.fields) {
      if (draft[field.id] !== values[field.id]) updates[field.id] = draft[field.id];
    }
    if (!Object.keys(updates).length) {
      setMessage("Nada para salvar nesta seção.");
      return;
    }
    setSaving(true);
    setMessage("Salvando alterações...");
    try {
      const result = await patchDashboardSettings(token, guildId, updates);
      setValues(result.values);
      setDraft(result.values);
      setMessage(`Alterações salvas: ${result.saved.length}.`);
      const summaryPayload = await fetchDashboardSummary(token, guildId);
      if (summaryPayload.ok) setSummary(summaryPayload.sections);
    } catch (error) {
      setMessage(cleanErrorText(error));
    } finally {
      setSaving(false);
    }
  }

  function updateDraft(field: DashboardFieldDefinition, raw: string | boolean) {
    setDraft((current) => ({ ...current, [field.id]: normalizeInputValue(field, raw) }));
  }

  useEffect(() => {
    let cancelled = false;
    bootstrapDiscord().then((result) => {
      if (cancelled) return;
      setBootstrap(result);
      if (!result.sdkReady) {
        setAuthState("error");
        setMessage("Abra esta página como Activity dentro do Discord.");
        return;
      }
      if (!result.context.guildId) {
        setAuthState("error");
        setMessage("Abra o dashboard dentro de um servidor para configurar o bot.");
        return;
      }
      if (!token) {
        setAuthState("needs_login");
        setMessage("Autorize sua conta para continuar.");
      }
    }).catch((error) => {
      if (cancelled) return;
      setAuthState("error");
      setMessage(cleanErrorText(error));
    });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!token || !isSnowflake(guildId)) return;
    void loadDashboard(token, guildId);
  }, [token, guildId]);

  const userName = bootstrap.currentUser.displayName || "Admin";
  const hasUnsaved = changedFields.length > 0;
  const selectedPercent = selectedSummary && selectedSummary.total > 0 ? Math.round((selectedSummary.configured / selectedSummary.total) * 100) : 0;
  const topModules = useMemo(
    () => [...summary]
      .sort((left, right) => Number(right.enabled === true) - Number(left.enabled === true) || right.configured - left.configured)
      .slice(0, 6),
    [summary],
  );
  const pendingSections = summary.filter((item) => item.total > item.configured).slice(0, 4);
  const changedFieldLabels = changedFields.map((field) => field.label).slice(0, 4);

  return (
    <main className="dashboard-shell">
      <header className="dashboard-topbar">
        <div className="brand-block">
          <span className="brand-icon">⚙️</span>
          <div>
            <strong>Dashboard</strong>
            <small>Painel administrativo do servidor</small>
          </div>
        </div>
        <div className="topbar-actions">
          <span className={`status-dot status-dot--${authState}`} />
          <span>{authState === "ready" ? "Conectado" : busy ? "Carregando" : "Aguardando"}</span>
        </div>
      </header>

      <section className="dashboard-hero">
        <div className="hero-copy">
          <p className="eyebrow">Discord Activity</p>
          <h1>Dashboard</h1>
          <p className="hero-text">Configure módulos, canais, permissões e automações do bot com uma experiência responsiva para celular e PC.</p>
          <div className="hero-meta">
            <span>Servidor: {guildId ?? "não identificado"}</span>
            <span>Admin: {userName}</span>
          </div>
        </div>
        <div className="hero-visual" aria-hidden="true">
          <span className="hero-orb hero-orb--one" />
          <span className="hero-orb hero-orb--two" />
          <span className="hero-orb hero-orb--three" />
          <div className="hero-glass-card">
            <strong>{dashboardStats.percent}%</strong>
            <small>configurado</small>
          </div>
        </div>
        <div className={`status-pill status-pill--${authState}`}>{message}</div>
      </section>

      {authState === "needs_login" && (
        <section className="auth-card">
          <div className="auth-icon">🔐</div>
          <h2>Entrar como administrador</h2>
          <p>O dashboard usa sua conta Discord apenas para validar se você pode configurar este servidor.</p>
          <button className="primary-button" disabled={busy} onClick={() => void login("consent")}>{busy ? "Autorizando..." : "Autorizar"}</button>
        </section>
      )}

      {authState === "denied" && (
        <section className="auth-card auth-card--danger">
          <div className="auth-icon">⛔</div>
          <h2>Sem permissão</h2>
          <p>Somente dono do servidor, administradores ou membros com Gerenciar servidor podem alterar configurações.</p>
          <button className="ghost-button" onClick={() => { clearCachedToken(); setToken(null); setAuthState("needs_login"); }}>Trocar conta</button>
        </section>
      )}

      {authState === "error" && (
        <section className="auth-card auth-card--danger">
          <div className="auth-icon">⚠️</div>
          <h2>Não foi possível abrir</h2>
          <p>{message}</p>
          <div className="button-row">
            <button className="ghost-button" onClick={() => void refreshDashboard()} disabled={!token || !guildId || busy}>Tentar de novo</button>
            <button className="ghost-button" onClick={() => { clearCachedToken(); setToken(null); setAuthState("needs_login"); }}>Reautenticar</button>
          </div>
        </section>
      )}

      {authState === "ready" && (
        <>
          <section className="stats-grid" aria-label="Resumo do dashboard">
            <article className="stat-card stat-card--wide">
              <span className="stat-icon">📊</span>
              <span className="stat-label">Configuração geral</span>
              <strong>{dashboardStats.percent}%</strong>
              <div className="progress-track"><span style={{ width: `${dashboardStats.percent}%` }} /></div>
            </article>
            <article className="stat-card"><span className="stat-icon">✅</span><span className="stat-label">Campos definidos</span><strong>{dashboardStats.configured}/{dashboardStats.totalFields}</strong></article>
            <article className="stat-card"><span className="stat-icon">🧩</span><span className="stat-label">Módulos ativos</span><strong>{dashboardStats.active}</strong></article>
            <article className="stat-card"><span className="stat-icon">⚠️</span><span className="stat-label">Pendências</span><strong>{dashboardStats.pending}</strong></article>
          </section>

          <section className="module-bento" aria-label="Módulos em destaque">
            {topModules.map((item) => (
              <button
                key={item.id}
                className={`module-card module-card--${statusClass(item)} ${selectedSection?.id === item.id ? "module-card--active" : ""}`}
                onClick={() => setSelectedSectionId(item.id)}
              >
                <span>{item.emoji}</span>
                <strong>{item.label}</strong>
                <small>{item.status}</small>
              </button>
            ))}
          </section>

          <section className="dashboard-grid">
            <aside className="section-list" aria-label="Áreas configuráveis">
              <div className="panel-title-row">
                <h2>Áreas</h2>
                <button className="ghost-button ghost-button--small" disabled={busy} onClick={() => void refreshDashboard()}>Atualizar</button>
              </div>
              <input className="search-input" value={query} placeholder="Buscar configuração..." onChange={(event) => setQuery(event.target.value)} />
              <div className="section-buttons">
                {filteredSections.map((item) => (
                  <button
                    key={item.id}
                    className={`section-button section-button--${statusClass(item)} ${selectedSection?.id === item.id ? "section-button--active" : ""}`}
                    onClick={() => setSelectedSectionId(item.id)}
                  >
                    <span className="section-emoji">{item.emoji}</span>
                    <span className="section-copy">
                      <strong>{item.label}</strong>
                      <small>{item.status} · {sectionConfiguredLabel(item)}</small>
                    </span>
                  </button>
                ))}
              </div>
            </aside>

            <section className="settings-panel">
              {selectedSection ? (
                <>
                  <div className="settings-heading">
                    <div>
                      <p className="eyebrow">{selectedSection.emoji} {selectedSection.label}</p>
                      <h2>{selectedSection.description}</h2>
                      <p>{selectedSummary ? `${selectedSummary.configured} de ${selectedSummary.total} campos configurados.` : ""}</p>
                    </div>
                    <div className="button-row">
                      <button className="ghost-button" onClick={() => setDraft(values)} disabled={!hasUnsaved || saving}>Desfazer</button>
                      <button className="primary-button" onClick={() => void saveSection()} disabled={!hasUnsaved || saving}>{saving ? "Salvando..." : hasUnsaved ? `Salvar ${changedFields.length}` : "Salvo"}</button>
                    </div>
                  </div>

                  <div className="section-overview">
                    <div>
                      <span className="stat-label">Progresso da seção</span>
                      <strong>{selectedPercent}%</strong>
                    </div>
                    <div className="progress-track"><span style={{ width: `${selectedPercent}%` }} /></div>
                    <span className={`mini-badge mini-badge--${statusClass(selectedSummary)}`}>{selectedSummary?.status ?? "carregando"}</span>
                  </div>

                  <div className="fields-grid">
                    {selectedSection.fields.map((field) => {
                      const current = draft[field.id];
                      const changed = draft[field.id] !== values[field.id];
                      return (
                        <label key={field.id} className={`field-card ${changed ? "field-card--changed" : ""}`}>
                          <span className="field-topline">
                            <span className="field-label">{field.label}</span>
                            {changed && <span className="mini-badge mini-badge--pending">alterado</span>}
                          </span>
                          {field.description && <span className="field-description">{field.description}</span>}
                          {field.type === "boolean" ? (
                            <span className="switch-row">
                              <input
                                type="checkbox"
                                checked={Boolean(current)}
                                onChange={(event) => updateDraft(field, event.target.checked)}
                              />
                              <span>{Boolean(current) ? "Ligado" : "Desligado"}</span>
                            </span>
                          ) : field.type === "select" ? (
                            <select value={stringifyValue(current)} onChange={(event) => updateDraft(field, event.target.value)}>
                              {(field.options ?? []).map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                            </select>
                          ) : field.type === "textarea" ? (
                            <textarea
                              value={stringifyValue(current)}
                              maxLength={field.maxLength}
                              placeholder={field.placeholder}
                              onChange={(event) => updateDraft(field, event.target.value)}
                            />
                          ) : (
                            <input
                              type={field.type === "number" ? "number" : "text"}
                              min={field.min}
                              max={field.max}
                              maxLength={field.maxLength}
                              value={stringifyValue(current)}
                              placeholder={field.placeholder ?? (field.type === "channel" ? "ID ou menção do canal" : field.type === "role" ? "ID ou menção do cargo" : field.type === "url" ? "https://..." : "")}
                              onChange={(event) => updateDraft(field, event.target.value)}
                            />
                          )}
                          <span className="field-current">Atual: {displayValue(field, values[field.id])}</span>
                        </label>
                      );
                    })}
                  </div>
                </>
              ) : (
                <div className="empty-state">Nenhuma seção carregada.</div>
              )}
            </section>

            <aside className="inspector-panel" aria-label="Resumo da seção selecionada">
              <div className="inspector-card inspector-card--primary">
                <span className="inspector-emoji">{selectedSection?.emoji ?? "⚙️"}</span>
                <p className="eyebrow">Seção atual</p>
                <h3>{selectedSection?.label ?? "Carregando"}</h3>
                <p>{selectedSection?.description ?? "Selecione uma área para configurar."}</p>
                <div className="progress-track"><span style={{ width: `${selectedPercent}%` }} /></div>
                <span className={`mini-badge mini-badge--${statusClass(selectedSummary)}`}>{selectedSummary?.status ?? "aguardando"}</span>
              </div>

              <div className="inspector-card">
                <div className="panel-title-row">
                  <h3>Alterações</h3>
                  <span className="mini-badge">{changedFields.length}</span>
                </div>
                {changedFieldLabels.length ? (
                  <ul className="change-list">
                    {changedFieldLabels.map((label) => <li key={label}>{label}</li>)}
                  </ul>
                ) : (
                  <p className="muted-text">Nenhuma alteração pendente nesta seção.</p>
                )}
              </div>

              <div className="inspector-card">
                <div className="panel-title-row">
                  <h3>Pendências</h3>
                  <span className="mini-badge mini-badge--pending">{dashboardStats.pending}</span>
                </div>
                {pendingSections.length ? (
                  <div className="pending-stack">
                    {pendingSections.map((item) => (
                      <button key={item.id} onClick={() => setSelectedSectionId(item.id)}>
                        <span>{item.emoji}</span>
                        <strong>{item.label}</strong>
                        <small>{item.configured}/{item.total}</small>
                      </button>
                    ))}
                  </div>
                ) : (
                  <p className="muted-text">Tudo que o dashboard conhece já está configurado.</p>
                )}
              </div>
            </aside>
          </section>

          {hasUnsaved && (
            <div className="save-dock">
              <span>{changedFields.length} alteração(ões) pendente(s) em {selectedSection?.label}.</span>
              <div className="button-row">
                <button className="ghost-button ghost-button--small" onClick={() => setDraft(values)} disabled={saving}>Descartar</button>
                <button className="primary-button primary-button--small" onClick={() => void saveSection()} disabled={saving}>Salvar agora</button>
              </div>
            </div>
          )}
        </>
      )}
    </main>
  );
}
