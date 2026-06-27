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
  const [customFields, setCustomFields] = useState([]);
  const [phoneNumbers, setPhoneNumbers] = useState([]);
  const [templates, setTemplates] = useState([]);
  const [flows, setFlows] = useState([]);
  const [campaigns, setCampaigns] = useState([]);
  const [inbox, setInbox] = useState([]);
  const [selectedConversation, setSelectedConversation] = useState(null);
  const [selectedContact, setSelectedContact] = useState(null);
  const [selectedTemplate, setSelectedTemplate] = useState(null);
  const [audience, setAudience] = useState({ included: 0, excluded: 0, receivers: 0 });
  const [leadDraft, setLeadDraft] = useState({ name: '', tags: [], lists: [], customFields: {} });
  const [toast, setToast] = useState('');

  const [meta, setMeta] = useState({ appId: '', appSecret: '', wabaId: '', phoneNumberId: '', accessToken: '', businessName: '' });
  const [contact, setContact] = useState({ name: '', phone: '', tags: '', lists: '', customFields: '' });
  const [newPhone, setNewPhone] = useState({ phoneNumberId: '', displayPhoneNumber: '', verifiedName: '' });
  const [newListName, setNewListName] = useState('');
  const [newField, setNewField] = useState({ key: '', label: '', type: 'text' });
  const [csvFile, setCsvFile] = useState(null);
  const [csvListName, setCsvListName] = useState('');
  const [csvTags, setCsvTags] = useState('');
  const [flow, setFlow] = useState({ name: '', triggerValue: '' });
  const [flowActions, setFlowActions] = useState([]);
  const [send, setSend] = useState({ name: '', listIds: [], templateName: '', language: 'pt_BR', responseFlowId: '', exclusionListIds: [], scheduledAt: '', sendNow: true, buttonFlowMap: {}, parameterMap: {}, phoneNumberId: '' });
  const [replyItems, setReplyItems] = useState([]);
  const [metaHydrated, setMetaHydrated] = useState(false);
  const metaHydratedRef = useRef(false);

  const notify = (msg) => {
    setToast(msg);
    setTimeout(() => setToast(''), 3500);
  };

  const load = async () => {
    const [h, s, d, c, l, tagRows, fieldRows, p, t, f, camp, i] = await Promise.all([
      http.get('/health').then((r) => r.data).catch(() => ({ ok: false })),
      http.get('/settings').then((r) => r.data).catch(() => ({})),
      http.get('/dashboard').then((r) => r.data).catch(() => ({})),
      http.get('/contacts').then((r) => r.data).catch(() => []),
      http.get('/lists').then((r) => r.data).catch(() => []),
      http.get('/tags').then((r) => r.data).catch(() => []),
      http.get('/custom-fields').then((r) => r.data).catch(() => []),
      http.get('/phone-numbers').then((r) => r.data).catch(() => []),
      http.get('/templates').then((r) => r.data).catch(() => []),
      http.get('/flows').then((r) => r.data).catch(() => []),
      http.get('/campaigns').then((r) => r.data).catch(() => []),
      http.get('/inbox').then((r) => r.data).catch(() => []),
    ]);
    setHealth(h); setSettings(s); setDashboard(d); setContacts(c); setLists(l); setTags(tagRows); setCustomFields(fieldRows); setPhoneNumbers(p); setTemplates(t); setFlows(f); setCampaigns(camp); setInbox(i);
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

  useEffect(() => {
    if (tab !== 'sends') return;
    http.post('/campaigns/estimate', {
      name: send.name || 'estimativa',
      templateName: send.templateName || 'template',
      language: send.language,
      listIds: send.listIds,
      exclusionListIds: send.exclusionListIds,
      buttonFlowMap: send.buttonFlowMap,
      parameterMap: send.parameterMap,
      phoneNumberId: send.phoneNumberId || null,
      sendNow: false,
    }).then((r) => setAudience(r.data)).catch(() => setAudience({ included: 0, excluded: 0, receivers: 0 }));
  }, [tab, send.listIds, send.exclusionListIds, send.templateName, send.language]);

  useEffect(() => {
    const contactRow = selectedConversation?.contact;
    if (!contactRow) {
      setLeadDraft({ name: '', tags: [], lists: [], customFields: {} });
      return;
    }
    setLeadDraft({
      name: contactRow.name || '',
      tags: contactRow.tags || [],
      lists: contactRow.lists || [],
      customFields: contactRow.customFields || {},
    });
  }, [selectedConversation?.contact?.id, selectedConversation?.contact?.updatedAt]);

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
  const qualityClass = (value) => {
    const normalized = String(value || '').toUpperCase();
    if (['GREEN', 'HIGH', 'GOOD'].includes(normalized)) return 'good';
    if (['YELLOW', 'MEDIUM', 'WARN', 'WARNING'].includes(normalized)) return 'warn';
    if (['RED', 'LOW', 'BAD', 'RESTRICTED', 'FLAGGED'].includes(normalized)) return 'danger';
    return 'neutral';
  };
  const selectedSendTemplate = templates.find((t) => t.name === send.templateName && (!send.language || t.language === send.language)) || templates.find((t) => t.name === send.templateName);
  const selectedTemplateButtons = selectedSendTemplate?.buttons || [];
  const selectedTemplateParams = selectedSendTemplate?.params || [];

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

  const syncPhoneNumbers = async () => {
    const { data } = await http.post('/phone-numbers/sync');
    notify(`${data.count} números sincronizados`);
    load();
  };

  const addPhoneNumber = async () => {
    await http.post('/phone-numbers', newPhone);
    setNewPhone({ phoneNumberId: '', displayPhoneNumber: '', verifiedName: '' });
    notify('Número cadastrado');
    load();
  };

  const activatePhone = async (id) => {
    await http.post(`/phone-numbers/${id}/activate`);
    notify('Número ativo atualizado');
    load();
  };

  const refreshPhone = async (id) => {
    await http.post(`/phone-numbers/${id}/refresh`);
    notify('Dados do número atualizados');
    load();
  };

  const deletePhone = async (id) => {
    await http.delete(`/phone-numbers/${id}`);
    notify('Número removido do sistema');
    load();
  };

  const createList = async () => {
    await http.post('/lists', { name: newListName });
    setNewListName('');
    notify('Lista salva');
    load();
  };

  const createCustomField = async () => {
    await http.post('/custom-fields', newField);
    setNewField({ key: '', label: '', type: 'text' });
    notify('Campo personalizado salvo');
    load();
  };

  const openContact = async (id) => {
    const { data } = await http.get(`/contacts/${id}`);
    setSelectedContact(data);
  };

  const openTemplate = async (id) => {
    const { data } = await http.get(`/templates/${id}`);
    setSelectedTemplate(data);
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
      listIds: send.listIds,
      templateName: send.templateName,
      language: send.language,
      responseFlowId: send.responseFlowId || null,
      exclusionListIds: send.exclusionListIds,
      buttonFlowMap: send.buttonFlowMap,
      parameterMap: send.parameterMap,
      phoneNumberId: send.phoneNumberId || null,
      scheduledAt: send.scheduledAt || null,
      sendNow: forceNow ?? send.sendNow,
    });
    notify((forceNow ?? send.sendNow) ? 'Envio iniciado' : 'Envio agendado');
    load();
  };

  const toggleArray = (field, id) => {
    setSend((current) => {
      const currentValues = new Set(current[field] || []);
      if (currentValues.has(id)) currentValues.delete(id);
      else currentValues.add(id);
      return { ...current, [field]: Array.from(currentValues) };
    });
  };

  const setButtonFlow = (buttonText, flowId) => {
    setSend((current) => ({ ...current, buttonFlowMap: { ...current.buttonFlowMap, [buttonText]: flowId } }));
  };

  const setParamField = (param, fieldKey) => {
    setSend((current) => ({ ...current, parameterMap: { ...current.parameterMap, [param]: fieldKey } }));
  };

  const openConversation = async (id) => {
    const { data } = await http.get(`/inbox/${id}`);
    setSelectedConversation(data);
  };

  const toggleLeadDraft = (field, id) => {
    setLeadDraft((current) => {
      const values = new Set(current[field] || []);
      if (values.has(id)) values.delete(id);
      else values.add(id);
      return { ...current, [field]: Array.from(values) };
    });
  };

  const setLeadCustomField = (key, value) => {
    setLeadDraft((current) => ({ ...current, customFields: { ...(current.customFields || {}), [key]: value } }));
  };

  const saveInboxLead = async () => {
    let contactId = selectedConversation?.contact?.id;
    if (!contactId) {
      const { data } = await http.post('/contacts', {
        phone: selectedConversation.conversation.phone,
        name: leadDraft.name || null,
        tags: [],
        lists: [],
        customFields: {},
      });
      contactId = data.id;
    }
    await http.patch(`/contacts/${contactId}`, leadDraft);
    notify('Dados do lead salvos');
    await openConversation(selectedConversation.conversation.id);
    load();
  };

  const windowLabel = (conversation) => {
    const raw = conversation?.conversation?.lastInboundAt;
    if (!raw) return 'sem janela';
    const closeAt = new Date(new Date(raw).getTime() + 24 * 60 * 60 * 1000);
    const ms = closeAt.getTime() - Date.now();
    if (ms <= 0) return 'fechada';
    const hours = Math.floor(ms / 3600000);
    const minutes = Math.floor((ms % 3600000) / 60000);
    return `${hours}h ${minutes}min restantes`;
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
          <div className="inline-actions"><Button onClick={saveMeta}>Salvar conexão</Button><Button variant="secondary" onClick={syncPhoneNumbers}>Sincronizar números</Button><Button variant="secondary" onClick={syncTemplates}>Sincronizar modelos</Button></div>
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
          <div className="subpanel">
            <h3>Números conectados</h3>
            <div className="form-row">
              <Field label="Phone Number ID"><input value={newPhone.phoneNumberId} onChange={(e) => setNewPhone({ ...newPhone, phoneNumberId: e.target.value })} placeholder="ID do número na Meta" /></Field>
              <Field label="Número exibido"><input value={newPhone.displayPhoneNumber} onChange={(e) => setNewPhone({ ...newPhone, displayPhoneNumber: e.target.value })} placeholder="+55..." /></Field>
              <Field label="Nome verificado"><input value={newPhone.verifiedName} onChange={(e) => setNewPhone({ ...newPhone, verifiedName: e.target.value })} /></Field>
              <Field label="Ação"><Button disabled={!newPhone.phoneNumberId} onClick={addPhoneNumber}>Adicionar número</Button></Field>
            </div>
            <div className="table">
              {phoneNumbers.length === 0 ? <p className="muted">Nenhum número sincronizado ainda.</p> : phoneNumbers.map((phone) => (
                <div className="row" key={phone.phoneNumberId || phone.id}>
                  <b>{phone.displayPhoneNumber || phone.phoneNumberId || phone.id}</b>
                  <span>{phone.verifiedName || 'sem nome verificado'}</span>
                  <span className={`quality-badge ${qualityClass(phone.qualityRating)}`}>Qualidade: {phone.qualityRating || 'UNKNOWN'}</span>
                  <span>Limite: {phone.messagingLimitTier || 'UNKNOWN'}</span>
                  <div className="row-actions">
                    <Button variant={phone.active ? 'primary' : 'secondary'} onClick={() => activatePhone(phone.phoneNumberId || phone.id)}>{phone.active ? 'Ativo' : 'Ativar'}</Button>
                    <Button variant="secondary" onClick={() => refreshPhone(phone.phoneNumberId || phone.id)}>Atualizar</Button>
                    <Button variant="secondary" onClick={() => deletePhone(phone.phoneNumberId || phone.id)}>Remover</Button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </section>}

        {tab === 'contacts' && <section className="panel stack">
          <h2>Contatos e listas</h2>
          <div className="two-col">
            <div className="subpanel">
              <h3>Salvar lista</h3>
              <Field label="Nome da lista"><input value={newListName} onChange={(e) => setNewListName(e.target.value)} /></Field>
              <Button disabled={!newListName} onClick={createList}>Salvar lista</Button>
              <p className="muted">{lists.length} listas cadastradas</p>
            </div>
            <div className="subpanel">
              <h3>Salvar campo personalizado</h3>
              <div className="composer-grid">
                <Field label="Chave"><input value={newField.key} onChange={(e) => setNewField({ ...newField, key: e.target.value })} placeholder="cidade" /></Field>
                <Field label="Rótulo"><input value={newField.label} onChange={(e) => setNewField({ ...newField, label: e.target.value })} placeholder="Cidade" /></Field>
              </div>
              <Button disabled={!newField.key} onClick={createCustomField}>Salvar campo</Button>
              <p className="muted">{customFields.length} campos cadastrados</p>
            </div>
          </div>
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
          <div className="two-col wide-left">
            <div className="table">{contacts.map((c) => <button className="row clickable" onClick={() => openContact(c.id)} key={c.id}><b>{c.name || 'Sem nome'}</b><span>{c.phone}</span><span>{(c.tags || []).map(tagName).join(', ') || '-'}</span><span>{(c.lists || []).map(listName).join(', ') || '-'}</span></button>)}</div>
            <div className="subpanel">
              <h3>Detalhes do contato</h3>
              {!selectedContact ? <p className="muted">Clique em um contato para ver dados completos.</p> : <>
                <b>{selectedContact.contact.name || 'Sem nome'}</b>
                <span>{selectedContact.contact.phone}</span>
                <p>Listas: {(selectedContact.contact.lists || []).map(listName).join(', ') || '-'}</p>
                <p>Tags: {(selectedContact.contact.tags || []).map(tagName).join(', ') || '-'}</p>
                <p>Campos: {Object.entries(selectedContact.contact.customFields || {}).map(([k, v]) => `${k}: ${v}`).join(' · ') || '-'}</p>
                <p className="muted">{selectedContact.messages.length} mensagens registradas</p>
              </>}
            </div>
          </div>
        </section>}

        {tab === 'templates' && <section className="panel stack">
          <div className="section-head"><h2>Modelos</h2><Button onClick={syncTemplates}><CloudArrowUp size={14} /> Sincronizar da Meta</Button></div>
          <div className="two-col wide-left">
            <div className="table">{templates.map((t) => <button className="row clickable" onClick={() => openTemplate(t.id)} key={t.id}><b>{t.name}</b><span>{t.language}</span><span>{t.category || '-'}</span><span>{t.status || 'manual'}</span></button>)}</div>
            <div className="subpanel">
              <h3>Conteúdo do modelo</h3>
              {!selectedTemplate ? <p className="muted">Clique em um modelo para ver corpo, botões e parâmetros.</p> : <>
                <b>{selectedTemplate.name}</b>
                <span>{selectedTemplate.category} · {selectedTemplate.language} · {selectedTemplate.status}</span>
                <p className="template-preview">{selectedTemplate.bodyPreview || '-'}</p>
                <p>Botões: {(selectedTemplate.buttons || []).map((b) => b.text).join(', ') || '-'}</p>
                <p>Parâmetros: {(selectedTemplate.params || []).map((p) => `{{${p}}}`).join(', ') || '-'}</p>
              </>}
            </div>
          </div>
        </section>}

        {tab === 'sends' && <section className="panel stack">
          <h2>Envios</h2>
          <div className="form-row">
            <Field label="Nome do envio"><input value={send.name} onChange={(e) => setSend({ ...send, name: e.target.value })} /></Field>
            <Field label="Número de envio"><select value={send.phoneNumberId} onChange={(e) => setSend({ ...send, phoneNumberId: e.target.value })}><option value="">Padrão ativo</option>{phoneNumbers.map((p) => <option key={p.phoneNumberId || p.id} value={p.phoneNumberId || p.id}>{p.displayPhoneNumber || p.phoneNumberId || p.id}</option>)}</select></Field>
            <Field label="Template"><select value={send.templateName} onChange={(e) => {
              const tpl = templates.find((t) => t.name === e.target.value);
              setSend({ ...send, templateName: e.target.value, language: tpl?.language || 'pt_BR', buttonFlowMap: {}, parameterMap: {} });
            }}><option value="">Selecione</option>{templates.map((t) => <option key={t.id} value={t.name}>{t.name} · {t.category || '-'} · {t.language}</option>)}</select></Field>
            <Field label="Agendar"><input type="datetime-local" value={send.scheduledAt} onChange={(e) => setSend({ ...send, scheduledAt: e.target.value, sendNow: false })} /></Field>
          </div>
          <div className="two-col">
            <div className="subpanel">
              <h3>Listas de destino</h3>
              <div className="check-list">{lists.map((l) => <label key={l.id}><input type="checkbox" checked={send.listIds.includes(l.id)} onChange={() => toggleArray('listIds', l.id)} /> {l.name}</label>)}</div>
            </div>
            <div className="subpanel">
              <h3>Listas de exclusão</h3>
              <div className="check-list">{lists.map((l) => <label key={l.id}><input type="checkbox" checked={send.exclusionListIds.includes(l.id)} onChange={() => toggleArray('exclusionListIds', l.id)} /> {l.name}</label>)}</div>
            </div>
          </div>
          {selectedSendTemplate && <div className="subpanel">
            <h3>{selectedSendTemplate.name} · {selectedSendTemplate.category}</h3>
            <p className="template-preview">{selectedSendTemplate.bodyPreview || '-'}</p>
            {selectedTemplateParams.length > 0 && <div className="form-row">
              {selectedTemplateParams.map((param) => <Field key={param} label={`Parâmetro {{${param}}}`}>
                <select value={send.parameterMap[param] || ''} onChange={(e) => setParamField(param, e.target.value)}>
                  <option value="">Selecione campo</option>
                  <option value="name">Nome do contato</option>
                  <option value="phone">Telefone</option>
                  {customFields.map((field) => <option key={field.key} value={field.key}>{field.label || field.key}</option>)}
                </select>
              </Field>)}
            </div>}
            {selectedTemplateButtons.length > 0 && <div className="form-row">
              {selectedTemplateButtons.map((button) => <Field key={button.text} label={`Fluxo: ${button.text}`}>
                <select value={send.buttonFlowMap[button.text] || ''} onChange={(e) => setButtonFlow(button.text, e.target.value)}>
                  <option value="">Nenhum</option>{flows.map((f) => <option key={f.id} value={f.id}>{f.name}</option>)}
                </select>
              </Field>)}
            </div>}
          </div>}
          <div className="grid mini-grid">
            <Metric icon={UsersThree} label="Selecionados" value={audience.included || 0} />
            <Metric icon={Tag} label="Excluídos" value={audience.excluded || 0} />
            <Metric icon={PaperPlaneTilt} label="Receberão" value={audience.receivers || 0} />
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
          <aside className="lead-side">
            <h2>Lead</h2>
            {!selectedConversation ? <p className="muted">Selecione uma conversa.</p> : <div className="lead-card vertical">
              <b>{selectedConversation.contact?.name || selectedConversation.conversation.phone}</b>
              <span>{selectedConversation.conversation.phone}</span>
              <span>Janela: {windowLabel(selectedConversation)}</span>
              <Field label="Nome"><input value={leadDraft.name} onChange={(e) => setLeadDraft({ ...leadDraft, name: e.target.value })} /></Field>
              <div className="subpanel compact">
                <h3>Listas</h3>
                <div className="check-list">{lists.length === 0 ? <span>Nenhuma lista cadastrada.</span> : lists.map((list) => <label key={list.id}><input type="checkbox" checked={(leadDraft.lists || []).includes(list.id)} onChange={() => toggleLeadDraft('lists', list.id)} /> {list.name}</label>)}</div>
              </div>
              <div className="subpanel compact">
                <h3>Tags</h3>
                <div className="check-list">{tags.length === 0 ? <span>Nenhuma tag cadastrada.</span> : tags.map((tag) => <label key={tag.id}><input type="checkbox" checked={(leadDraft.tags || []).includes(tag.id)} onChange={() => toggleLeadDraft('tags', tag.id)} /> {tag.name}</label>)}</div>
              </div>
              {customFields.length > 0 && <div className="subpanel compact">
                <h3>Campos</h3>
                {customFields.map((field) => <Field key={field.key} label={field.label || field.key}><input value={(leadDraft.customFields || {})[field.key] || ''} onChange={(e) => setLeadCustomField(field.key, e.target.value)} /></Field>)}
              </div>}
              <Button onClick={saveInboxLead}>Salvar lead</Button>
            </div>}
          </aside>
          <div className="conversation main-conversation">
            {!selectedConversation ? <p className="muted">Selecione uma conversa.</p> : <>
              <div className="messages">{selectedConversation.messages.map((m) => <div key={m.id} className={`bubble ${m.direction}`}><span>{m.text || m.type}</span><small>{new Date(m.createdAt).toLocaleString('pt-BR')}</small></div>)}</div>
              <SequenceEditor items={replyItems} setItems={setReplyItems} notify={notify} />
              <Button onClick={reply} disabled={replyItems.length === 0}>Responder</Button>
            </>}
          </div>
          <div>
            <h2>Conversas</h2>
            <div className="inbox-list">{inbox.map((c) => <button key={c.id} onClick={() => openConversation(c.id)} className={selectedConversation?.conversation?.id === c.id ? 'selected' : ''}><b>{c.name || c.phone}</b><span>{c.lastMessage?.text || c.lastMessage?.type || '-'}</span></button>)}</div>
          </div>
        </section>}
      </section>
      {toast && <div className="toast">{toast}</div>}
    </main>
  );
}

createRoot(document.getElementById('root')).render(<App />);
