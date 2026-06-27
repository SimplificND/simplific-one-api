import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import axios from 'axios';
import {
  ChatCircleText,
  CheckCircle,
  CloudArrowUp,
  Database,
  FlowArrow,
  Gear,
  GitBranch,
  PaperPlaneTilt,
  Plus,
  Robot,
  Tag,
  UploadSimple,
  UsersThree,
} from '@phosphor-icons/react';
import './styles.css';

const API = `${import.meta.env.VITE_BACKEND_URL || import.meta.env.REACT_APP_BACKEND_URL || ''}/api`;
const http = axios.create({ baseURL: API });

function Button({ children, variant = 'primary', ...props }) {
  return <button className={`btn ${variant}`} {...props}>{children}</button>;
}

function Field({ label, children }) {
  return <label className="field"><span>{label}</span>{children}</label>;
}

function Metric({ icon: Icon, label, value }) {
  return <div className="metric"><Icon size={22} weight="duotone" /><div><span>{label}</span><strong>{value}</strong></div></div>;
}

function SequenceEditor({ items, setItems, notify }) {
  const [type, setType] = useState('send_message');
  const [text, setText] = useState('');
  const [mediaUrl, setMediaUrl] = useState('');
  const [caption, setCaption] = useState('');
  const [delaySeconds, setDelaySeconds] = useState(0);

  const upload = async (file) => {
    if (!file) return;
    const form = new FormData();
    form.append('file', file);
    const { data } = await http.post('/media', form);
    setMediaUrl(data.url);
    notify?.('Mídia carregada');
  };

  const add = () => {
    const action = { type, text, mediaUrl, caption, delaySeconds: Number(delaySeconds || 0), tags: [], lists: [] };
    if (type === 'delay') action.delaySeconds = Number(delaySeconds || 0);
    if (type === 'add_tags') action.tags = text.split(',').map((x) => x.trim()).filter(Boolean);
    if (type === 'add_lists') action.lists = text.split(',').map((x) => x.trim()).filter(Boolean);
    if (type === 'send_message' && !text.trim()) return;
    if (['image', 'video', 'audio', 'document'].includes(type) && !mediaUrl.trim()) return;
    setItems([...items, action]);
    setText('');
    setMediaUrl('');
    setCaption('');
    setDelaySeconds(0);
  };

  return (
    <div className="sequence">
      <div className="sequence-list">
        {items.length === 0 ? <p className="muted">Nenhuma ação no fluxo.</p> : items.map((item, index) => (
          <div className="sequence-item" key={`${item.type}-${index}`}>
            <b>{index + 1}. {item.type}</b>
            <span>{item.text || item.caption || item.mediaUrl || item.tags?.join(', ') || item.lists?.join(', ') || `${item.delaySeconds}s`}</span>
            <button onClick={() => setItems(items.filter((_, i) => i !== index))}>remover</button>
          </div>
        ))}
      </div>
      <div className="composer-grid">
        <Field label="Ação">
          <select value={type} onChange={(e) => setType(e.target.value)}>
            <option value="send_message">Enviar mensagem</option>
            <option value="audio">Enviar áudio</option>
            <option value="image">Enviar imagem</option>
            <option value="video">Enviar vídeo</option>
            <option value="document">Enviar arquivo</option>
            <option value="add_tags">Adicionar tags</option>
            <option value="add_lists">Adicionar listas</option>
            <option value="delay">Atraso inteligente</option>
          </select>
        </Field>
        <Field label="Atraso em segundos">
          <input type="number" min="0" value={delaySeconds} onChange={(e) => setDelaySeconds(e.target.value)} />
        </Field>
      </div>
      {['image', 'video', 'audio', 'document'].includes(type) && (
        <>
          <Field label="Arquivo do dispositivo">
            <input type="file" onChange={(e) => upload(e.target.files?.[0])} />
          </Field>
          <Field label="URL da mídia">
            <input value={mediaUrl} onChange={(e) => setMediaUrl(e.target.value)} placeholder="https://..." />
          </Field>
          <Field label="Legenda">
            <textarea value={caption} onChange={(e) => setCaption(e.target.value)} />
          </Field>
        </>
      )}
      {type === 'send_message' && (
        <Field label="Mensagem">
          <textarea value={text} onChange={(e) => setText(e.target.value)} />
        </Field>
      )}
      {['add_tags', 'add_lists'].includes(type) && (
        <Field label={type === 'add_tags' ? 'Tags separadas por vírgula' : 'Listas separadas por vírgula'}>
          <input value={text} onChange={(e) => setText(e.target.value)} />
        </Field>
      )}
      <Button variant="secondary" onClick={add}><Plus size={14} /> Adicionar</Button>
    </div>
  );
}

function App() {
  const [tab, setTab] = useState('overview');
  const [health, setHealth] = useState(null);
  const [settings, setSettings] = useState({});
  const [dashboard, setDashboard] = useState({});
  const [contacts, setContacts] = useState([]);
  const [lists, setLists] = useState([]);
  const [tags, setTags] = useState([]);
  const [templates, setTemplates] = useState([]);
  const [flows, setFlows] = useState([]);
  const [campaigns, setCampaigns] = useState([]);
  const [inbox, setInbox] = useState([]);
  const [selectedConversation, setSelectedConversation] = useState(null);
  const [toast, setToast] = useState('');

  const [meta, setMeta] = useState({ appId: '', appSecret: '', wabaId: '', phoneNumberId: '', accessToken: '', businessName: '' });
  const [contact, setContact] = useState({ name: '', phone: '', tags: '', lists: '', customFields: '' });
  const [csvFile, setCsvFile] = useState(null);
  const [csvListName, setCsvListName] = useState('');
  const [csvTags, setCsvTags] = useState('');
  const [flow, setFlow] = useState({ name: '', triggerValue: '' });
  const [flowActions, setFlowActions] = useState([]);
  const [send, setSend] = useState({ name: '', listId: '', templateName: '', language: 'pt_BR', responseFlowId: '', exclusionListIds: '', scheduledAt: '', sendNow: true });
  const [replyItems, setReplyItems] = useState([]);
  const [metaHydrated, setMetaHydrated] = useState(false);
  const metaHydratedRef = useRef(false);

  const notify = (msg) => {
    setToast(msg);
    setTimeout(() => setToast(''), 3500);
  };

  const load = async () => {
    const [h, s, d, c, l, tagRows, t, f, camp, i] = await Promise.all([
      http.get('/health').then((r) => r.data).catch(() => ({ ok: false })),
      http.get('/settings').then((r) => r.data).catch(() => ({})),
      http.get('/dashboard').then((r) => r.data).catch(() => ({})),
      http.get('/contacts').then((r) => r.data).catch(() => []),
      http.get('/lists').then((r) => r.data).catch(() => []),
      http.get('/tags').then((r) => r.data).catch(() => []),
      http.get('/templates').then((r) => r.data).catch(() => []),
      http.get('/flows').then((r) => r.data).catch(() => []),
      http.get('/campaigns').then((r) => r.data).catch(() => []),
      http.get('/inbox').then((r) => r.data).catch(() => []),
    ]);
    setHealth(h); setSettings(s); setDashboard(d); setContacts(c); setLists(l); setTags(tagRows); setTemplates(t); setFlows(f); setCampaigns(camp); setInbox(i);
    if (!metaHydratedRef.current && s.meta) {
      setMeta((old) => ({ ...old, ...s.meta, accessToken: old.accessToken }));
      metaHydratedRef.current = true;
      setMetaHydrated(true);
    }
  };

  useEffect(() => {
    load();
    const timer = setInterval(load, 8000);
    return () => clearInterval(timer);
  }, []);

  const nav = useMemo(() => [
    ['overview', Database, 'Visão Geral'],
    ['connection', Gear, 'Conexão'],
    ['contacts', UsersThree, 'Contatos'],
    ['templates', GitBranch, 'Modelos'],
    ['sends', PaperPlaneTilt, 'Envios'],
    ['flows', FlowArrow, 'Fluxos'],
    ['inbox', ChatCircleText, 'Inbox'],
  ], []);

  const listName = (id) => lists.find((x) => x.id === id)?.name || id;
  const tagName = (id) => tags.find((x) => x.id === id)?.name || id;

  const saveMeta = async () => {
    await http.post('/meta/settings', meta);
    notify('Conexão salva');
    metaHydratedRef.current = false;
    setMetaHydrated(false);
    load();
  };

  const updateMeta = (key, value) => {
    metaHydratedRef.current = true;
    setMetaHydrated(true);
    setMeta((current) => ({ ...current, [key]: value }));
  };

  const copyText = async (value, label) => {
    await navigator.clipboard.writeText(value);
    notify(`${label} copiado`);
  };

  const syncTemplates = async () => {
    const { data } = await http.post('/meta/sync-templates');
    notify(`${data.count} modelos sincronizados`);
    load();
  };

  const createContact = async () => {
    const customFields = {};
    contact.customFields.split(',').map((x) => x.trim()).filter(Boolean).forEach((pair) => {
      const [k, ...rest] = pair.split(':');
      if (k) customFields[k.trim()] = rest.join(':').trim();
    });
    await http.post('/contacts', {
      name: contact.name || null,
      phone: contact.phone,
      tags: contact.tags.split(',').map((x) => x.trim()).filter(Boolean),
      lists: contact.lists.split(',').map((x) => x.trim()).filter(Boolean),
      customFields,
    });
    setContact({ name: '', phone: '', tags: '', lists: '', customFields: '' });
    notify('Contato salvo');
    load();
  };

  const importCsv = async () => {
    const form = new FormData();
    form.append('file', csvFile);
    form.append('listName', csvListName);
    form.append('tags', csvTags);
    const { data } = await http.post('/contacts/import-csv', form);
    notify(`${data.count} leads importados`);
    setCsvFile(null);
    load();
  };

  const createFlow = async () => {
    await http.post('/flows', { ...flow, actions: flowActions, enabled: true });
    setFlow({ name: '', triggerValue: '' });
    setFlowActions([]);
    notify('Fluxo salvo');
    load();
  };

  const createSend = async (forceNow = null) => {
    await http.post('/campaigns', {
      name: send.name,
      listIds: send.listId ? [send.listId] : [],
      templateName: send.templateName,
      language: send.language,
      responseFlowId: send.responseFlowId || null,
      exclusionListIds: send.exclusionListIds.split(',').map((x) => x.trim()).filter(Boolean),
      scheduledAt: send.scheduledAt || null,
      sendNow: forceNow ?? send.sendNow,
    });
    notify((forceNow ?? send.sendNow) ? 'Envio iniciado' : 'Envio agendado');
    load();
  };

  const openConversation = async (id) => {
    const { data } = await http.get(`/inbox/${id}`);
    setSelectedConversation(data);
  };

  const reply = async () => {
    await http.post(`/inbox/${selectedConversation.conversation.id}/reply`, {
      phone: selectedConversation.conversation.phone,
      items: replyItems.map((item) => ({
        type: item.type === 'send_message' ? 'text' : item.type,
        text: item.text,
        mediaUrl: item.mediaUrl,
        caption: item.caption,
        delaySeconds: item.delaySeconds || 0,
      })),
    });
    setReplyItems([]);
    notify('Resposta enviada');
    openConversation(selectedConversation.conversation.id);
  };

  return (
    <main className="shell">
      <aside>
        <div className="brand"><div className="mark">S</div><div><b>Simplific</b><span>ONE | API</span></div></div>
        <nav>{nav.map(([id, Icon, label]) => <button key={id} className={tab === id ? 'active' : ''} onClick={() => setTab(id)}><Icon size={16} /> {label}</button>)}</nav>
      </aside>
      <section className="content">
        <header>
          <p>// MOTOR OFICIAL WHATSAPP</p>
          <h1>Simplific ONE API</h1>
          <div className="status-row">
            <span className={health?.ok ? 'status ok' : 'status'}>{health?.ok ? 'backend online' : 'backend offline'}</span>
            <span className={health?.metaConfigured ? 'status ok' : 'status warn'}>{health?.metaConfigured ? 'número conectado' : 'conexão pendente'}</span>
          </div>
        </header>

        {tab === 'overview' && <>
          <section className="grid">
            <Metric icon={UsersThree} label="Contatos" value={dashboard.contacts || 0} />
            <Metric icon={GitBranch} label="Listas" value={dashboard.lists || 0} />
            <Metric icon={Tag} label="Tags" value={dashboard.tags || 0} />
            <Metric icon={PaperPlaneTilt} label="Envios" value={dashboard.campaigns || 0} />
            <Metric icon={ChatCircleText} label="Conversas não lidas" value={dashboard.inboxUnread || 0} />
            <Metric icon={Robot} label="Automações rodadas" value={dashboard.automationRuns || 0} />
          </section>
          <section className="panel"><div><h2>Pronto para configurar</h2><p>Conecte o número oficial, sincronize modelos, importe leads e crie fluxos de resposta.</p></div><ul className="checks"><li><CheckCircle /> Webhook: /api/meta/webhook</li><li><CheckCircle /> Token: simplific-one-api-webhook</li><li><CheckCircle /> Mídias por upload com URL pública</li></ul></section>
        </>}

        {tab === 'connection' && <section className="panel stack">
          <h2>Conectar número oficial</h2>
          <div className="form-row">
            <Field label="Nome da empresa"><input value={meta.businessName || ''} onChange={(e) => updateMeta('businessName', e.target.value)} /></Field>
            <Field label="App ID"><input value={meta.appId || ''} onChange={(e) => updateMeta('appId', e.target.value)} /></Field>
            <Field label="App Secret"><input value={meta.appSecret || ''} onChange={(e) => updateMeta('appSecret', e.target.value)} /></Field>
            <Field label="WABA ID"><input value={meta.wabaId || ''} onChange={(e) => updateMeta('wabaId', e.target.value)} /></Field>
            <Field label="Phone Number ID"><input value={meta.phoneNumberId || ''} onChange={(e) => updateMeta('phoneNumberId', e.target.value)} /></Field>
            <Field label="Access Token"><input value={meta.accessToken || ''} onChange={(e) => updateMeta('accessToken', e.target.value)} placeholder={settings.meta?.accessTokenPreview || ''} /></Field>
          </div>
          <div className="inline-actions"><Button onClick={saveMeta}>Salvar conexão</Button><Button variant="secondary" onClick={syncTemplates}>Sincronizar modelos</Button></div>
          <div className="copy-grid">
            <div className="notice">
              <span>Callback URL</span>
              <code>{window.location.origin}/api/meta/webhook</code>
              <Button variant="secondary" onClick={() => copyText(`${window.location.origin}/api/meta/webhook`, 'Webhook')}>Copiar URL</Button>
            </div>
            <div className="notice">
              <span>Verify token</span>
              <code>simplific-one-api-webhook</code>
              <Button variant="secondary" onClick={() => copyText('simplific-one-api-webhook', 'Token')}>Copiar token</Button>
            </div>
          </div>
        </section>}

        {tab === 'contacts' && <section className="panel stack">
          <h2>Contatos e listas</h2>
          <div className="subpanel">
            <h3>Importar CSV</h3>
            <div className="form-row">
              <Field label="Arquivo CSV"><input type="file" accept=".csv,text/csv" onChange={(e) => setCsvFile(e.target.files?.[0])} /></Field>
              <Field label="Salvar na lista"><input value={csvListName} onChange={(e) => setCsvListName(e.target.value)} placeholder="ex: Lançamento Julho" /></Field>
              <Field label="Tags padrão"><input value={csvTags} onChange={(e) => setCsvTags(e.target.value)} placeholder="lead, origem-instagram" /></Field>
            </div>
            <Button disabled={!csvFile} onClick={importCsv}><UploadSimple size={14} /> Subir leads</Button>
          </div>
          <div className="subpanel">
            <h3>Contato manual</h3>
            <div className="form-row">
              <Field label="Nome"><input value={contact.name} onChange={(e) => setContact({ ...contact, name: e.target.value })} /></Field>
              <Field label="Telefone"><input value={contact.phone} onChange={(e) => setContact({ ...contact, phone: e.target.value })} /></Field>
              <Field label="Tags"><input value={contact.tags} onChange={(e) => setContact({ ...contact, tags: e.target.value })} /></Field>
              <Field label="Listas"><input value={contact.lists} onChange={(e) => setContact({ ...contact, lists: e.target.value })} /></Field>
              <Field label="Campos personalizados"><input value={contact.customFields} onChange={(e) => setContact({ ...contact, customFields: e.target.value })} placeholder="cidade:SP, produto:VIP" /></Field>
            </div>
            <Button onClick={createContact}>Salvar contato</Button>
          </div>
          <div className="table">{contacts.map((c) => <div className="row" key={c.id}><b>{c.name || 'Sem nome'}</b><span>{c.phone}</span><span>{(c.tags || []).map(tagName).join(', ') || '-'}</span><span>{(c.lists || []).map(listName).join(', ') || '-'}</span></div>)}</div>
        </section>}

        {tab === 'templates' && <section className="panel stack">
          <div className="section-head"><h2>Modelos</h2><Button onClick={syncTemplates}><CloudArrowUp size={14} /> Sincronizar da Meta</Button></div>
          <div className="table">{templates.map((t) => <div className="row" key={t.id}><b>{t.name}</b><span>{t.language}</span><span>{t.status || 'manual'}</span><span>{t.bodyPreview || '-'}</span></div>)}</div>
        </section>}

        {tab === 'sends' && <section className="panel stack">
          <h2>Envios</h2>
          <div className="form-row">
            <Field label="Nome do envio"><input value={send.name} onChange={(e) => setSend({ ...send, name: e.target.value })} /></Field>
            <Field label="Lista"><select value={send.listId} onChange={(e) => setSend({ ...send, listId: e.target.value })}><option value="">Todos os contatos</option>{lists.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}</select></Field>
            <Field label="Template"><select value={send.templateName} onChange={(e) => setSend({ ...send, templateName: e.target.value })}><option value="">Selecione</option>{templates.map((t) => <option key={t.id} value={t.name}>{t.name} · {t.language}</option>)}</select></Field>
            <Field label="Fluxo de resposta"><select value={send.responseFlowId} onChange={(e) => setSend({ ...send, responseFlowId: e.target.value })}><option value="">Nenhum</option>{flows.map((f) => <option key={f.id} value={f.id}>{f.name}</option>)}</select></Field>
            <Field label="Listas de exclusão"><input value={send.exclusionListIds} onChange={(e) => setSend({ ...send, exclusionListIds: e.target.value })} placeholder="IDs separados por vírgula" /></Field>
            <Field label="Agendar"><input type="datetime-local" value={send.scheduledAt} onChange={(e) => setSend({ ...send, scheduledAt: e.target.value, sendNow: false })} /></Field>
          </div>
          <div className="inline-actions"><Button onClick={() => createSend(true)} disabled={!send.name || !send.templateName}>Disparar agora</Button><Button variant="secondary" onClick={() => createSend(false)} disabled={!send.name || !send.templateName}>Salvar/agendar</Button></div>
          <div className="table">{campaigns.map((c) => <div className="row" key={c.id}><b>{c.name}</b><span>{c.templateName}</span><span>{c.status}</span><span>{c.sent || 0}/{c.targetCount || 0}</span></div>)}</div>
        </section>}

        {tab === 'flows' && <section className="panel stack">
          <h2>Construção de fluxo</h2>
          <div className="form-row"><Field label="Nome do fluxo"><input value={flow.name} onChange={(e) => setFlow({ ...flow, name: e.target.value })} /></Field><Field label="Botão/gatilho esperado"><input value={flow.triggerValue} onChange={(e) => setFlow({ ...flow, triggerValue: e.target.value })} /></Field></div>
          <SequenceEditor items={flowActions} setItems={setFlowActions} notify={notify} />
          <Button onClick={createFlow} disabled={!flow.name}>Salvar fluxo</Button>
          <div className="table">{flows.map((f) => <div className="row" key={f.id}><b>{f.name}</b><span>{f.triggerValue || '-'}</span><span>{(f.actions || []).length} ações</span><span>{f.enabled ? 'ativo' : 'pausado'}</span></div>)}</div>
        </section>}

        {tab === 'inbox' && <section className="panel inbox-panel">
          <div>
            <h2>Inbox</h2>
            <div className="inbox-list">{inbox.map((c) => <button key={c.id} onClick={() => openConversation(c.id)} className={selectedConversation?.conversation?.id === c.id ? 'selected' : ''}><b>{c.name || c.phone}</b><span>{c.lastMessage?.text || c.lastMessage?.type || '-'}</span></button>)}</div>
          </div>
          <div className="conversation">
            {!selectedConversation ? <p className="muted">Selecione uma conversa.</p> : <>
              <div className="lead-card"><b>{selectedConversation.contact?.name || selectedConversation.conversation.phone}</b><span>{selectedConversation.conversation.phone}</span><span>Janela: {selectedConversation.window?.open ? 'aberta' : 'fechada'}</span><span>Tags: {(selectedConversation.contact?.tags || []).map(tagName).join(', ') || '-'}</span><span>Listas: {(selectedConversation.contact?.lists || []).map(listName).join(', ') || '-'}</span><span>Campos: {Object.entries(selectedConversation.contact?.customFields || {}).map(([k, v]) => `${k}: ${v}`).join(' · ') || '-'}</span></div>
              <div className="messages">{selectedConversation.messages.map((m) => <div key={m.id} className={`bubble ${m.direction}`}><span>{m.text || m.type}</span><small>{new Date(m.createdAt).toLocaleString('pt-BR')}</small></div>)}</div>
              <SequenceEditor items={replyItems} setItems={setReplyItems} notify={notify} />
              <Button onClick={reply} disabled={replyItems.length === 0}>Responder</Button>
            </>}
          </div>
        </section>}
      </section>
      {toast && <div className="toast">{toast}</div>}
    </main>
  );
}

createRoot(document.getElementById('root')).render(<App />);
