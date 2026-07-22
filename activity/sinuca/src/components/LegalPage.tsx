import { ArrowLeft, LockKeyhole, ScrollText } from "lucide-react";
import { Brand } from "./BrowserLanding";

interface LegalPageProps {
  kind: "privacy" | "terms";
  onBack(): void;
}

export function LegalPage({ kind, onBack }: LegalPageProps) {
  const privacy = kind === "privacy";
  return (
    <div className="osk-legal-shell">
      <header><button className="osk-brand-button" onClick={onBack}><Brand /></button><button className="osk-secondary-button osk-secondary-button--small" onClick={onBack}><ArrowLeft size={15} /> Voltar</button></header>
      <main className="osk-legal-card">
        <span className="osk-legal-icon">{privacy ? <LockKeyhole size={22} /> : <ScrollText size={22} />}</span>
        <span className="osk-kicker">Osaka · Painel web</span>
        <h1>{privacy ? "Política de privacidade" : "Termos de uso"}</h1>
        <p className="osk-legal-updated">Última revisão: 22 de julho de 2026</p>
        {privacy ? <PrivacyContent /> : <TermsContent />}
      </main>
    </div>
  );
}

function PrivacyContent() {
  return <div className="osk-legal-content">
    <h2>Dados tratados</h2><p>O painel usa a autenticação do Discord para identificar sua conta, listar servidores que você pode administrar e verificar as permissões necessárias. As configurações salvas pertencem ao servidor selecionado.</p>
    <h2>Sessão e credenciais</h2><p>Os tokens de acesso do Discord ficam protegidos no backend. O navegador recebe apenas um cookie de sessão seguro, sem acesso por JavaScript. Senhas do Discord não são recebidas ou armazenadas pelo painel.</p>
    <h2>Uso das informações</h2><p>Os dados são usados apenas para autenticação, autorização, exibição dos servidores e aplicação das configurações solicitadas. O painel não vende dados pessoais.</p>
    <h2>Retenção e segurança</h2><p>Sessões expiram automaticamente e podem ser encerradas pelo botão de sair. Registros técnicos mínimos podem ser mantidos para diagnóstico, segurança e prevenção de abuso, sem registrar códigos OAuth ou tokens.</p>
    <h2>Controle do usuário</h2><p>Você pode encerrar sua sessão a qualquer momento. A remoção do bot de um servidor interrompe novas operações do painel nesse servidor.</p>
  </div>;
}

function TermsContent() {
  return <div className="osk-legal-content">
    <h2>Uso autorizado</h2><p>O painel deve ser usado apenas em servidores nos quais sua conta tenha permissão de administração ou gerenciamento compatível. Toda operação continua sujeita às regras do Discord.</p>
    <h2>Responsabilidade pelas configurações</h2><p>Você é responsável por revisar canais, cargos, mensagens e permissões antes de salvar. O painel aplica apenas alterações explicitamente confirmadas.</p>
    <h2>Disponibilidade</h2><p>O serviço pode passar por manutenção ou ficar temporariamente indisponível. Recursos podem ser ajustados para preservar segurança, compatibilidade e estabilidade do bot.</p>
    <h2>Uso indevido</h2><p>Não é permitido tentar contornar permissões, explorar falhas, automatizar requisições abusivas ou utilizar o serviço para violar direitos, leis ou políticas da plataforma.</p>
    <h2>Alterações</h2><p>Estes termos podem ser atualizados quando houver mudanças relevantes no funcionamento do painel. A versão vigente será publicada nesta página.</p>
  </div>;
}
