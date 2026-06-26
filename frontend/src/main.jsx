import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import axios from 'axios';
import { ChatCircleText, Database, GitBranch, PaperPlaneTilt, Robot, Tag, UsersThree } from '@phosphor-icons/react';
import './styles.css';

const API = `${import.meta.env.REACT_APP_BACKEND_URL || ''}/api`;

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

function App() {
  const [health, setHealth] = useState(null);
  const [dashboard, setDashboard] = useState(null);

  useEffect(() => {
    axios.get(`${API}/health`).then((r) => setHealth(r.data)).catch(() => setHealth({ ok: false }));
    axios.get(`${API}/dashboard`).then((r) => setDashboard(r.data)).catch(() => setDashboard(null));
  }, []);

  const data = dashboard || {};

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
          <a className="active"><Database size={16} /> Visão Geral</a>
          <a><UsersThree size={16} /> Contatos</a>
          <a><PaperPlaneTilt size={16} /> Campanhas</a>
          <a><ChatCircleText size={16} /> Inbox</a>
          <a><Robot size={16} /> Automações</a>
        </nav>
      </aside>

      <section className="content">
        <header>
          <p>// MOTOR OFICIAL WHATSAPP</p>
          <h1>Simplific ONE API</h1>
          <span className={health?.ok ? 'status ok' : 'status'}>{health?.ok ? 'backend online' : 'backend offline'}</span>
        </header>

        <section className="grid">
          <Metric icon={UsersThree} label="Contatos" value={data.contacts || 0} />
          <Metric icon={GitBranch} label="Listas" value={data.lists || 0} />
          <Metric icon={Tag} label="Tags" value={data.tags || 0} />
          <Metric icon={PaperPlaneTilt} label="Campanhas" value={data.campaigns || 0} />
          <Metric icon={ChatCircleText} label="Inbox" value={data.inboxUnread || 0} />
          <Metric icon={Robot} label="Automações" value={data.automationRuns || 0} />
        </section>

        <section className="panel">
          <div>
            <h2>Primeiro núcleo</h2>
            <p>Conexão Meta, templates, listas, inbox e automações por resposta/botão.</p>
          </div>
          <ol>
            <li>Configurar credenciais da Meta Cloud API</li>
            <li>Validar webhook oficial</li>
            <li>Importar leads e criar listas</li>
            <li>Enviar campanha com template</li>
            <li>Receber respostas no Inbox</li>
          </ol>
        </section>
      </section>
    </main>
  );
}

createRoot(document.getElementById('root')).render(<App />);

