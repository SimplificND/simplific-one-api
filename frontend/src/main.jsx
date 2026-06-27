import React, { useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import axios from 'axios';
import {
  ChatCircleText,
  CheckCircle,
  Database,
  GitBranch,
  PaperPlaneTilt,
  Plus,
  Robot,
  Tag,
  UsersThree,
  WarningCircle,
} from '@phosphor-icons/react';
import './styles.css';

const API = `${import.meta.env.VITE_BACKEND_URL || import.meta.env.REACT_APP_BACKEND_URL || ''}/api`;

const http = axios.create({ baseURL: API });

function Button({ children, variant = 'primary', ...props }) {
  return <button className={`btn ${variant}`} {...props}>{children}</button>;
}

function Field({ label, children }) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
    </label>
  );
}

function Metric({ icon: Icon, label, value }) {
  return (
    <div className="metric">
      <Icon size={22} weight="duotone" />
      <div>
        <span>{label}</span>
        <strong>{value}</strong>
      </div>
    </div>
  );
}

function SequenceEditor({ items, setItems }) {
  const [type, setType] = useState('text');
  const [text, setText] = useState('');
  const [templateName, setTemplateName] = useState('');
  const [mediaUrl, setMediaUrl] = useState('');
  const [caption, setCaption] = useState('');
  const [delaySeconds, setDelaySeconds] = useState(0);

  const add = () => {
    const item = { type, text, templateName, mediaUrl, caption, language: 'pt_BR', delaySeconds: Number(delaySeconds || 0) };
    if (type === 'text' && !text.trim()) return;
    if (type === 'template' && !templateName.trim()) return;
    if (['image', 'video', 'audio', 'document'].includes(type) && !mediaUrl.trim()) return;
    setItems([...items, item]);
    setText('');
    setTemplateName('');
    setMediaUrl('');
    setCaption('');
    setDelaySeconds(0);
  };

  return (
    <div className="sequence">
      <div className="sequence-list">
        {items.length === 0 ? (
          <p className="muted">Nenhuma mensagem na sequência.</p>
        ) : items.map((item, index) => (
          <div className="sequence-item" key={`${item.type}-${index}`}>
            <b>{index + 1}. {item.type}</b>
            <span>{item.text || item.caption || item.templateName || item.mediaUrl}</span>
            <button onClick={() => setItems(items.filter((_, i) => i !== index))}>remover</button>
          </div>
        ))}
      </div>

      <div className="composer-grid">
        <Field label="Tipo">
          <select value={type} onChange={(e) => setType(e.target.value)}>
            <option value="text">Texto</option>
            <option value="template">Template</option>
            <option value="image">Imagem</option>
            <option value="video">Vídeo</option>
            <option value="audio">Áudio</option>
            <option value="document">Arquivo</option>
          </select>
        </Field>
        <Field label="Delay antes de enviar">
          <input type="number" min="0" value={delaySeconds} onChange={(e) => setDelaySeconds(e.target.value)} />
        </Field>
      </div>

      {type === 'template' ? (
        <Field label="Nome do template aprovado">
          <input value={templateName} onChange={(e) => setTemplateName(e.target.value)} placeholder="ex: confirmacao_agendamento" />
        </Field>
      ) : type === 'text' ? (
        <Field label="Mensagem">
          <textarea value={text} onChange={(e) => setText(e.target.value)} placeholder="Escreva a mensagem..." />
        </Field>
      ) : (
        <>
          <Field label="URL pública da mídia">
            <input value={mediaUrl} onChange={(e) => setMediaUrl(e.target.value)} placeholder="https://..." />
          </Field>
          <Field label="Legenda">
            <textarea value={caption} onChange={(e) => setCaption(e.target.value)} placeholder="Opcional" />
          </Field>
        </>
      )}
      <Button variant="secondary" onClick={add}><Plus size={14} /> Adicionar à sequência</Button>
    </div>
  );
}

function App() {
  const [tab, setTab] = useState('overview');
  const [health, setHealth] = useState(null);
  const [dashboard, setDashboard] = useState(null);
  const [contacts, setContacts] = useState([]);
  const [inbox, setInbox] = useState([]);
  const [automations, setAutomations] = useState([]);
  const [toast, setToast] = useState('');

  const [contact, setContact] = useState({ name: '', phone: '', tags: '', lists: '' });
  const [sendPhone, setSendPhone] = useState('');
  const [sendItems, setSendItems] = useState([]);
  const [autoItems, setAutoItems] = useState([]);
  const [automation, setAutomation] = useState({ name: '', triggerType: 'contains', triggerValue: '', addTags: '', addLists: '' });

  const load = async () => {
    const [h, d, c, i, a] = await Promise.all([
      http.get('/health').then((r) => r.data).catch(() => ({ ok: false })),
      http.get('/dashboard').then((r) => r.data).catch(() => null),
      http.get('/contacts').then((r) => r.data).catch(() => []),
      http.get('/inbox').then((r) => r.data).catch(() => []),
      http.get('/automations').then((r) => r.data).catch(() => []),
    ]);
    setHealth(h);
    setDashboard(d);
    setContacts(c);
    setInbox(i);
    setAutomations(a);
  };

  useEffect(() => {
    load();
    const timer = setInterval(load, 8000);
    return () => clearInterval(timer);
  }, []);

  const notify = (msg) => {
    setToast(msg);
    setTimeout(() => setToast(''), 3500);
  };

  const createContact = async () => {
    await http.post('/contacts', {
      name: contact.name || null,
      phone: contact.phone,
      tags: contact.tags.split(',').map((x) => x.trim()).filter(Boolean),
      lists: contact.lists.split(',').map((x) => x.trim()).filter(Boolean),
    });
    setContact({ name: '', phone: '', tags: '', lists: '' });
    notify('Contato salvo');
    load();
  };

  const sendManual = async () => {
    await http.post('/messages/send', { phone: sendPhone, items: sendItems });
    setSendItems([]);
    notify('Sequência enviada/registrada');
    load();
  };

  const createAutomation = async () => {
    await http.post('/automations', {
      name: automation.name,
      triggerType: automation.triggerType,
      triggerValue: automation.triggerValue,
      addTags: automation.addTags.split(',').map((x) => x.trim()).filter(Boolean),
      addLists: automation.addLists.split(',').map((x) => x.trim()).filter(Boolean),
      items: autoItems,
      enabled: true,
    });
    setAutomation({ name: '', triggerType: 'contains', triggerValue: '', addTags: '', addLists: '' });
    setAutoItems([]);
    notify('Automação criada');
    load();
  };

  const data = dashboard || {};
  const nav = useMemo(() => [
    ['overview', Database, 'Visão Geral'],
    ['contacts', UsersThree, 'Contatos'],
    ['send', PaperPlaneTilt, 'Enviar'],
    ['inbox', ChatCircleText, 'Inbox'],
    ['automations', Robot, 'Automações'],
  ], []);

  return (
    <main className="shell">
      <aside>
        <div className="brand">
          <div className="mark">S</div>
          <div>
            <b>Simplific</b>
            <span>ONE | API</span>
          </div>
        </div>
        <nav>
          {nav.map(([id, Icon, label]) => (
            <button key={id} className={tab === id ? 'active' : ''} onClick={() => setTab(id)}>
              <Icon size={16} /> {label}
            </button>
          ))}
        </nav>
      </aside>

      <section className="content">
        <header>
          <p>// MOTOR OFICIAL WHATSAPP</p>
          <h1>Simplific ONE API</h1>
          <div className="status-row">
            <span className={health?.ok ? 'status ok' : 'status'}>{health?.ok ? 'backend online' : 'backend offline'}</span>
            <span className={health?.metaConfigured ? 'status ok' : 'status warn'}>
              {health?.metaConfigured ? 'Meta configurada' : 'Meta pendente'}
            </span>
          </div>
        </header>

        {tab === 'overview' && (
          <>
            <section className="grid">
              <Metric icon={UsersThree} label="Contatos" value={data.contacts || 0} />
              <Metric icon={GitBranch} label="Listas" value={data.lists || 0} />
              <Metric icon={Tag} label="Tags" value={data.tags || 0} />
              <Metric icon={PaperPlaneTilt} label="Campanhas" value={data.campaigns || 0} />
              <Metric icon={ChatCircleText} label="Conversas não lidas" value={data.inboxUnread || 0} />
              <Metric icon={Robot} label="Automações rodadas" value={data.automationRuns || 0} />
            </section>
            <section className="panel">
              <div>
                <h2>Próximo núcleo</h2>
                <p>Começamos pelo essencial para utility template + resposta automática: webhook, inbox, contatos, tags e sequências.</p>
              </div>
              <ul className="checks">
                <li><CheckCircle /> Webhook oficial preparado</li>
                <li><CheckCircle /> Inbox salva mensagens recebidas</li>
                <li><CheckCircle /> Sequência texto/template/mídia</li>
                <li><WarningCircle /> Precisa das credenciais Meta para envio real</li>
              </ul>
            </section>
          </>
        )}

        {tab === 'contacts' && (
          <section className="panel stack">
            <h2>Contatos</h2>
            <div className="form-row">
              <Field label="Nome"><input value={contact.name} onChange={(e) => setContact({ ...contact, name: e.target.value })} /></Field>
              <Field label="Telefone"><input value={contact.phone} onChange={(e) => setContact({ ...contact, phone: e.target.value })} /></Field>
              <Field label="Tags"><input value={contact.tags} onChange={(e) => setContact({ ...contact, tags: e.target.value })} placeholder="vip, lead-quente" /></Field>
              <Field label="Listas"><input value={contact.lists} onChange={(e) => setContact({ ...contact, lists: e.target.value })} placeholder="lancamento" /></Field>
            </div>
            <Button onClick={createContact}>Salvar contato</Button>
            <div className="table">
              {contacts.map((c) => (
                <div className="row" key={c.id}>
                  <b>{c.name || 'Sem nome'}</b>
                  <span>{c.phone}</span>
                  <span>{(c.tags || []).join(', ') || '-'}</span>
                  <span>{(c.lists || []).join(', ') || '-'}</span>
                </div>
              ))}
            </div>
          </section>
        )}

        {tab === 'send' && (
          <section className="panel stack">
            <h2>Envio manual</h2>
            <Field label="Telefone do lead">
              <input value={sendPhone} onChange={(e) => setSendPhone(e.target.value)} placeholder="5511999999999" />
            </Field>
            <SequenceEditor items={sendItems} setItems={setSendItems} />
            <Button onClick={sendManual} disabled={!sendPhone || sendItems.length === 0}>
              <PaperPlaneTilt size={14} /> Enviar sequência
            </Button>
          </section>
        )}

        {tab === 'inbox' && (
          <section className="panel stack">
            <h2>Inbox</h2>
            <div className="table">
              {inbox.length === 0 ? <p className="muted">Nenhuma conversa recebida ainda.</p> : inbox.map((c) => (
                <div className="row" key={c.id}>
                  <b>{c.name || c.phone}</b>
                  <span>{c.lastMessage?.text || c.lastMessage?.type || '-'}</span>
                  <span>{c.unread ? `${c.unread} não lida(s)` : 'lida'}</span>
                  <span>{new Date(c.lastMessageAt).toLocaleString('pt-BR')}</span>
                </div>
              ))}
            </div>
          </section>
        )}

        {tab === 'automations' && (
          <section className="panel stack">
            <h2>Automações</h2>
            <div className="form-row">
              <Field label="Nome"><input value={automation.name} onChange={(e) => setAutomation({ ...automation, name: e.target.value })} /></Field>
              <Field label="Gatilho">
                <select value={automation.triggerType} onChange={(e) => setAutomation({ ...automation, triggerType: e.target.value })}>
                  <option value="contains">Texto contém</option>
                  <option value="exact">Texto exato</option>
                  <option value="button">Botão clicado</option>
                  <option value="any">Qualquer mensagem</option>
                </select>
              </Field>
              <Field label="Valor"><input value={automation.triggerValue} onChange={(e) => setAutomation({ ...automation, triggerValue: e.target.value })} /></Field>
              <Field label="Tags ao ativar"><input value={automation.addTags} onChange={(e) => setAutomation({ ...automation, addTags: e.target.value })} /></Field>
            </div>
            <SequenceEditor items={autoItems} setItems={setAutoItems} />
            <Button onClick={createAutomation} disabled={!automation.name}>Criar automação</Button>
            <div className="table">
              {automations.map((a) => (
                <div className="row" key={a.id}>
                  <b>{a.name}</b>
                  <span>{a.triggerType}: {a.triggerValue || 'qualquer'}</span>
                  <span>{(a.items || []).length} mensagem(ns)</span>
                  <span>{a.enabled ? 'ativa' : 'pausada'}</span>
                </div>
              ))}
            </div>
          </section>
        )}
      </section>

      {toast && <div className="toast">{toast}</div>}
    </main>
  );
}

createRoot(document.getElementById('root')).render(<App />);
