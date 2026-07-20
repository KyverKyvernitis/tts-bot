from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import discord
from discord.ext import commands

try:
    from zoneinfo import ZoneInfo

    DAILY_TIMEZONE = ZoneInfo("America/Sao_Paulo")
except Exception:  # pragma: no cover - fallback para ambientes sem tzdata
    DAILY_TIMEZONE = timezone.utc


FUN_INPUT_MAX_CHARS = 500
FUN_OPTION_MAX_COUNT = 10
FUN_OPTION_MAX_CHARS = 120
SYSTEM_RANDOM = random.SystemRandom()


@dataclass(frozen=True, slots=True)
class SubcommandSpec:
    name: str
    summary: str
    usage: str
    example: str
    category: str
    aliases: tuple[str, ...] = ()


TOOL_SPECS: tuple[SubcommandSpec, ...] = (
    SubcommandSpec(
        name="help",
        summary="Abre esta central ou mostra os detalhes de um subcomando.",
        usage="_cmd help [subcomando]",
        example="_cmd help julgamento",
        category="Ferramentas",
    ),
    SubcommandSpec(
        name="dm",
        summary="Envia uma mensagem privada para um usuário pelo ID.",
        usage="_cmd dm <id_do_usuário> <mensagem>",
        example="_cmd dm 123456789012345678 Oi!",
        category="Ferramentas",
    ),
    SubcommandSpec(
        name="canal",
        summary="Envia uma mensagem em um canal pelo ID.",
        usage="_cmd canal <id_do_canal> <mensagem>",
        example="_cmd canal 123456789012345678 Boa noite!",
        category="Ferramentas",
        aliases=("channel",),
    ),
    SubcommandSpec(
        name="nano",
        summary="Abre o editor visual de arquivos pelo Discord.",
        usage="_cmd nano <arquivo>",
        example="_cmd nano cogs/terminal_cmd.py",
        category="Ferramentas",
        aliases=("vim", "vi", "micro", "edit"),
    ),
)


FUN_SPECS: tuple[SubcommandSpec, ...] = (
    SubcommandSpec(
        name="julgamento",
        summary="Abre um tribunal informal para um membro.",
        usage="_cmd julgamento [ID, menção ou nome]",
        example="_cmd julgamento @Core",
        category="Diversão",
    ),
    SubcommandSpec(
        name="oraculo",
        summary="Consulta uma entidade dramática sobre uma pergunta comum.",
        usage="_cmd oraculo <pergunta>",
        example="_cmd oraculo Eu devo sair de casa hoje?",
        category="Diversão",
    ),
    SubcommandSpec(
        name="sorte",
        summary="Calcula a sorte diária de um membro.",
        usage="_cmd sorte [ID, menção ou nome]",
        example="_cmd sorte @Core",
        category="Diversão",
    ),
    SubcommandSpec(
        name="batalha",
        summary="Simula uma disputa curta entre dois membros.",
        usage="_cmd batalha <membro 1> | <membro 2>",
        example="_cmd batalha @Core | @João",
        category="Diversão",
    ),
    SubcommandSpec(
        name="escolher",
        summary="Escolhe uma opção e revela a consequência.",
        usage="_cmd escolher <opção 1> | <opção 2> [| opção 3]",
        example="_cmd escolher pizza | hambúrguer | ficar com fome",
        category="Diversão",
    ),
    SubcommandSpec(
        name="moeda",
        summary="Joga uma moeda com um nível desnecessário de drama.",
        usage="_cmd moeda",
        example="_cmd moeda",
        category="Diversão",
    ),
    SubcommandSpec(
        name="titulo",
        summary="Concede um título diário com habilidade e desvantagem.",
        usage="_cmd titulo [ID, menção ou nome]",
        example="_cmd titulo @Core",
        category="Diversão",
    ),
    SubcommandSpec(
        name="raridade",
        summary="Transforma uma pessoa ou coisa em item de jogo.",
        usage="_cmd raridade <texto, ID, menção ou nome>",
        example="_cmd raridade meu carregador",
        category="Diversão",
    ),
    SubcommandSpec(
        name="boleto",
        summary="Emite uma cobrança cenográfica por um delito cotidiano.",
        usage="_cmd boleto [ID, menção ou nome]",
        example="_cmd boleto @Core",
        category="Diversão",
    ),
    SubcommandSpec(
        name="cafe",
        summary="Mede a capacidade diária de funcionar sem voltar para a cama.",
        usage="_cmd cafe [ID, menção ou nome]",
        example="_cmd cafe @Core",
        category="Diversão",
    ),
)


ALL_SPECS: tuple[SubcommandSpec, ...] = TOOL_SPECS + FUN_SPECS
SPEC_BY_NAME: dict[str, SubcommandSpec] = {}
for _spec in ALL_SPECS:
    SPEC_BY_NAME[_spec.name] = _spec
    for _alias in _spec.aliases:
        SPEC_BY_NAME[_alias] = _spec


JUDGEMENTS: tuple[tuple[str, str, str], ...] = (
    (
        "JUDGEMENT!",
        "disse **“tô chegando”** enquanto ainda procurava o chinelo",
        "ficar responsável por cobrar o Pix do churrasco, inclusive de quem jurou que já pagou",
    ),
    (
        "PREPARE THYSELF!",
        "pegou a última coxinha e ainda perguntou **“ninguém queria, né?”**",
        "levar os salgados no próximo encontro e não abrir a caixa antes de chegar",
    ),
    (
        "Despite everything, it’s still you.",
        "deixou uma colher de arroz na panela só para não precisar lavar",
        "lavar a panela, a tampa e o pote misterioso que está na pia desde ontem",
    ),
    (
        "Glory to Arstotzka.",
        "chegou ao churrasco sem carne, sem gelo e perguntando se precisava levar alguma coisa",
        "ter a entrada negada até retornar com dois sacos de gelo e uma justificativa aceitável",
    ),
    (
        "THY END IS NOW!",
        "falou **“só mais um episódio”** às duas da manhã sabendo que o despertador tocaria às seis",
        "encarar o alarme sem usar a função soneca mais de sete vezes",
    ),
    (
        "NO COST TOO GREAT.",
        "aceitou dividir a conta antes de descobrir quem pediu sobremesa, entrada e bebida importada",
        "abrir a calculadora e enfrentar as consequências sem arredondar para baixo",
    ),
    (
        "There is no escape.",
        "visualizou a mensagem da família e achou que poderia fingir que não viu",
        "responder no grupo e confirmar presença no almoço de domingo",
    ),
    (
        "A brawl is surely brewing!",
        "guardou a panela inteira na geladeira com exatamente duas colheres de comida",
        "transferir tudo para um pote e devolver a prateleira ocupada ao povo",
    ),
)


ORACLE_RESULTS: tuple[tuple[str, str, str], ...] = (
    (
        "There is no escape.",
        "**Não.** Você pode até tentar fugir, mas alguém já escreveu seu nome na lista de confirmados.",
        "O destino recomenda levar uma sobremesa para reduzir o dano.",
    ),
    (
        "Your choices don’t matter.",
        "A decisão já foi tomada pelo grupo. A pergunta serviu apenas para manter a aparência de democracia.",
        "Prepare-se para ir ao mesmo lugar de sempre.",
    ),
    (
        "Despite everything, it’s still you.",
        "**Sim.** Contra toda lógica, a situação parece favorável.",
        "Não conte vantagem antes de acontecer; o universo gosta de uma segunda fase.",
    ),
    (
        "MACHINE, TURN BACK NOW.",
        "**Não hoje.** Recuar agora evita um compromisso extra, uma fila e alguém dizendo “é rapidinho”.",
        "Voltar para casa também é uma rota válida.",
    ),
    (
        "NO COST TOO GREAT.",
        "**Talvez.** Vai funcionar, mas o preço pode envolver dinheiro, paciência ou o último pedaço de pizza.",
        "Leia as condições antes de aceitar.",
    ),
    (
        "Glory to Arstotzka.",
        "**Aprovado com ressalvas.** Seus documentos estão em ordem; suas decisões, nem tanto.",
        "Entrada permitida. Não faça o fiscal se arrepender.",
    ),
    (
        "You feel an evil presence watching you...",
        "**Provavelmente.** Algo está prestes a acontecer, e existe uma chance razoável de ser só o entregador parado no portão.",
        "Mantenha o celular por perto.",
    ),
    (
        "A brawl is surely brewing!",
        "**Sim**, mas alguém vai discordar com uma confiança impressionante e nenhum argumento.",
        "Escolha suas palavras antes que a música de chefe comece.",
    ),
)


BATTLE_SCENES: tuple[tuple[str, str, str], ...] = (
    (
        "+PARRY\n+COUNTER",
        "{loser} apresentou argumentos.\n{winner} apresentou uma captura de tela com data, horário e contexto completo.",
        "A discussão terminou antes de desbloquear a segunda fase.",
    ),
    (
        "A brawl is surely brewing!",
        "{loser} entrou na disputa com confiança de protagonista.\n{winner} respondeu apenas: **“depois a gente conversa”**.",
        "A frase causou dano crítico e ansiedade prolongada.",
    ),
    (
        "JUDGEMENT!",
        "{loser} alegou que estava dormindo.\n{winner} exibiu o registro de **“online há 3 minutos”**.",
        "A defesa pediu intervalo e nunca mais voltou.",
    ),
    (
        "There is no escape.",
        "{loser} tentou encerrar o assunto.\n{winner} respondeu: **“e tem outra coisa...”**.",
        "Segunda fase iniciada sem possibilidade de pular a cena.",
    ),
    (
        "THY END IS NOW!",
        "{loser} ficou com o último pedaço de pizza.\n{winner} lembrou quem pagou a maior parte do pedido.",
        "O pedaço mudou de dono por decisão unânime.",
    ),
    (
        "Glory to Arstotzka.",
        "{loser} chegou sem comprovante.\n{winner} trouxe recibo, protocolo e uma testemunha do grupo da família.",
        "Entrada negada para a versão dos fatos de {loser}.",
    ),
)


CHOICE_CONSEQUENCES: tuple[tuple[str, str], ...] = (
    (
        "Rota pacífica confirmada.",
        "Nenhum compromisso foi ferido durante esta decisão.",
    ),
    (
        "Your choices don’t matter.",
        "O grupo vai discutir por quarenta minutos e terminar escolhendo exatamente isso.",
    ),
    (
        "NO COST TOO GREAT.",
        "A opção parece ótima. O frete, a taxa e o adicional noturno discordam.",
    ),
    (
        "There is no escape.",
        "Você escolheu livremente uma coisa que já estava decidida desde ontem.",
    ),
    (
        "A brawl is surely brewing!",
        "Alguém vai discordar em três, dois, um... pronto, começou.",
    ),
    (
        "Glory to Arstotzka.",
        "Escolha autorizada. A entrada depende de levar gelo e não chegar de mãos vazias.",
    ),
    (
        "+ULTRARICOSHOT",
        "A decisão ricocheteou em todas as opções sensatas e acertou justamente a mais cara.",
    ),
    (
        "Despite everything, it’s still you.",
        "No fundo você já queria essa opção e só precisava de alguém para assumir a culpa.",
    ),
)


TITLES: tuple[tuple[str, str, str], ...] = (
    (
        "Guardião do Banco Inconvenientemente Distante",
        "encontra um lugar para descansar somente depois de atravessar o reino inteiro",
        "o banco continua longe",
    ),
    (
        "Fiscal Supremo do Pix do Churrasco",
        "lembra exatamente quem comeu, quem bebeu e quem sumiu na hora de pagar",
        "precisa mandar a mensagem **“faltou só você”** sem parecer agressivo",
    ),
    (
        "Portador da Sacola que Rasga no Portão",
        "carrega todas as compras em uma viagem por orgulho",
        "a experiência adquirida é principalmente emocional",
    ),
    (
        "Cavaleiro da Última Coxinha",
        "detecta salgado restante a três mesas de distância",
        "todo mundo percebe quando a habilidade é ativada",
    ),
    (
        "Guardião Oficial do Balde do Gelo",
        "sabe onde está o gelo mesmo quando ninguém mais sabe",
        "ouve **“cadê o gelo?”** a cada quatro minutos",
    ),
    (
        "Herdeiro do Controle Sem Pilha",
        "faz qualquer aparelho funcionar depois de bater duas vezes",
        "a solução deixa de funcionar quando outra pessoa tenta",
    ),
    (
        "Lenda da Tomada Livre",
        "encontra uma tomada em aeroporto, rodoviária e festa de família",
        "sempre esquece o carregador",
    ),
    (
        "Campeão da Soneca de Cinco Minutos",
        "fecha os olhos às 14h e abre às 18h com total confiança",
        "ninguém acredita que foi sem querer",
    ),
)


RARITY_ITEMS: tuple[tuple[str, str, str, str], ...] = (
    (
        "Carregador de posição específica",
        "Lendário",
        "funciona apenas quando inclinado a 43 graus e ninguém respira perto da tomada",
        "+2 paciência\n-4 mobilidade",
    ),
    (
        "Guarda-chuva preventivo",
        "Épico",
        "aparece cinco minutos depois de começar a chover",
        "+6 proteção tardia\n-2 previsão",
    ),
    (
        "Pote de sorvete com feijão",
        "Mítico",
        "causa esperança ao abrir e dano emocional imediato",
        "+8 armazenamento\n-10 confiança",
    ),
    (
        "Chinelo oficial de matar pernilongo",
        "Relíquia",
        "recebe precisão perfeita somente depois que o inseto desaparece",
        "+7 alcance\n+1 barulho de ameaça",
    ),
    (
        "Sacola resistente de mercado",
        "Lendária",
        "sobrevive à escada, ao portão e à compra do mês sem abandonar uma garrafa",
        "+9 carga\nNO COST TOO GREAT.",
    ),
    (
        "Controle com uma pilha boa e outra suspeita",
        "Raro",
        "mantém 12% de bateria por um período cientificamente inexplicável",
        "+3 esperança\n-3 confiabilidade",
    ),
    (
        "Fone sem lado marcado",
        "Amaldiçoado",
        "é colocado certo somente depois de três tentativas e uma música inteira",
        "+4 persistência\nYour choices don’t matter.",
    ),
    (
        "Tampa que serve em outro pote",
        "Incomum",
        "encaixa perfeitamente em tudo, menos no recipiente que você precisa",
        "+5 versatilidade\n-6 utilidade imediata",
    ),
)


BILLS: tuple[tuple[str, str, str], ...] = (
    (
        "aceitou um acordo sem ler as condições",
        "R$ 13,66",
        "O contrato tinha letras pequenas, três fases e uma música alegre demais para ser confiável.",
    ),
    (
        "comprou um produto de R$ 19,99 com frete de R$ 42,90",
        "R$ 62,89",
        "NO COST TOO GREAT.",
    ),
    (
        "disse “tô chegando” e saiu de casa quarenta minutos depois",
        "R$ 27,40",
        "A multa cresce a cada mensagem perguntando **“cadê você?”**.",
    ),
    (
        "guardou a panela inteira na geladeira com uma colher de comida",
        "R$ 18,75",
        "A cobrança inclui aluguel da prateleira e danos à paz doméstica.",
    ),
    (
        "perdeu para o mesmo chefe onze vezes e culpou o controle",
        "R$ 11,11",
        "A décima segunda tentativa permanece disponível e financeiramente irresponsável.",
    ),
    (
        "chegou ao churrasco sem gelo e perguntou se ainda precisava comprar",
        "R$ 24,00",
        "Glory to Arstotzka. Entrada pendente até regularização da carga.",
    ),
    (
        "abriu o aplicativo do banco, viu a fatura e fechou como se nada tivesse acontecido",
        "R$ 39,90",
        "There is no escape.",
    ),
    (
        "comeu a batata dos outros depois de afirmar que não estava com fome",
        "R$ 16,50",
        "A taxa de conveniência emocional já está incluída.",
    ),
)


LUCK_BONUSES: tuple[str, ...] = (
    "o ônibus aparece quando você chega ao ponto",
    "a fila ao lado realmente anda mais rápido",
    "um Pix inesperado entra antes do almoço",
    "a tomada livre fica exatamente perto da sua cadeira",
    "o pão de queijo vem quente e sem estar oco",
    "a promoção aplica sem exigir cadastro, cupom e um ritual",
    "o entregador encontra o endereço sem ligar três vezes",
    "+PARRY: você rebate uma conversa longa com um simples **“beleza 👍”**",
)


LUCK_PENALTIES: tuple[str, ...] = (
    "o boleto continua sendo seu",
    "a última coxinha desaparece enquanto você pega o refrigerante",
    "o celular chega a 7% justamente quando você sai de casa",
    "o guarda-chuva fica em casa no único dia em que chove",
    "You feel an evil presence watching you... é só o grupo da família digitando",
    "o frete custa mais do que a compra",
)


COFFEE_STATES: tuple[tuple[range, str, str, tuple[str, ...]], ...] = (
    (
        range(0, 26),
        "MACHINE, TURN BACK NOW.",
        "O organismo abriu, analisou a situação e recomendou voltar para a cama.",
        ("levantar sem negociar com o travesseiro", "lembrar por que entrou no cômodo"),
    ),
    (
        range(26, 51),
        "Combustível em reserva",
        "Funciona por ameaça, compromisso marcado e medo de perder o horário.",
        ("responder mensagens curtas", "esquentar algo sem esquecer no micro-ondas"),
    ),
    (
        range(51, 76),
        "Rotina liberada",
        "Energia suficiente para começar o dia e fingir organização até o almoço.",
        ("regar as plantas", "organizar o inventário", "pescar por tempo demais"),
    ),
    (
        range(76, 91),
        "+PARRY",
        "O sono foi rebatido com sucesso. Ele provavelmente volta na segunda fase.",
        ("encarar uma fila", "resolver uma pendência", "sobreviver ao grupo da família"),
    ),
    (
        range(91, 101),
        "MANKIND IS DEAD. BLOOD IS FUEL. HELL IS FULL.",
        "O café está pronto e a pessoa agora acredita que consegue resolver a vida inteira antes das dez.",
        ("limpar a casa", "sair sem atrasar", "cobrar o Pix sem hesitar"),
    ),
)


def _clean_input(value: str, limit: int = FUN_INPUT_MAX_CHARS) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return text[:limit]


def _safe_display(value: Any, limit: int = FUN_INPUT_MAX_CHARS) -> str:
    text = _clean_input(str(value or ""), limit)
    text = discord.utils.escape_mentions(text)
    return discord.utils.escape_markdown(text)


def _daily_rng(command: str, key: str) -> random.Random:
    day = datetime.now(DAILY_TIMEZONE).date().isoformat()
    digest = hashlib.sha256(f"terminal-fun|{day}|{command}|{key}".encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:16], "big"))


def _progress_bar(value: int, *, blocks: int = 10) -> str:
    value = max(0, min(100, int(value)))
    filled = max(0, min(blocks, round((value / 100) * blocks)))
    return "█" * filled + "░" * (blocks - filled)


def _looks_like_user_reference(value: str) -> bool:
    text = str(value or "").strip()
    return bool(re.fullmatch(r"<@!?\d+>", text) or text.isdigit())


def _avatar_url(user: Any) -> str | None:
    avatar = getattr(user, "display_avatar", None)
    url = getattr(avatar, "url", None)
    return str(url) if url else None


class TerminalCardView(discord.ui.LayoutView):
    def __init__(
        self,
        title: str,
        body: str,
        *,
        color: discord.Colour | int | None = None,
        thumbnail_url: str | None = None,
        thumbnail_description: str | None = None,
        footer: str | None = None,
    ):
        super().__init__(timeout=None)
        content = f"## {title}\n{str(body or '').strip()}".strip()
        children: list[discord.ui.Item] = []
        if thumbnail_url:
            children.append(
                discord.ui.Section(
                    discord.ui.TextDisplay(content[:3900]),
                    accessory=discord.ui.Thumbnail(
                        thumbnail_url,
                        description=(thumbnail_description or "Avatar do membro")[:256],
                    ),
                )
            )
        else:
            children.append(discord.ui.TextDisplay(content[:3900]))
        if footer:
            children.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
            children.append(discord.ui.TextDisplay(f"-# {str(footer).strip()[:500]}"))
        self.add_item(discord.ui.Container(*children, accent_color=color or discord.Colour.blurple()))


class _HelpButton(discord.ui.Button):
    def __init__(self, view_ref: "TerminalHelpView", page: str, label: str, emoji: str):
        super().__init__(
            label=label,
            emoji=emoji,
            style=discord.ButtonStyle.primary if view_ref.page == page else discord.ButtonStyle.secondary,
        )
        self.view_ref = view_ref
        self.page = page

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view_ref.switch_page(interaction, self.page)


class TerminalHelpView(discord.ui.LayoutView):
    PAGE_META: tuple[tuple[str, str, str], ...] = (
        ("overview", "Visão geral", "🧰"),
        ("tools", "Ferramentas", "🛠️"),
        ("fun", "Diversão", "🎭"),
        ("usage", "Como usar", "📖"),
    )

    def __init__(self, router: "TerminalFunRouter", owner_id: int, *, page: str = "overview"):
        super().__init__(timeout=180.0)
        self.router = router
        self.owner_id = int(owner_id)
        self.page = page
        self.message: discord.Message | None = None
        self.rebuild()

    def rebuild(self) -> None:
        self.clear_items()
        content = self.router.help_page_content(self.page)
        row = discord.ui.ActionRow(*(_HelpButton(self, page, label, emoji) for page, label, emoji in self.PAGE_META))
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(content),
                discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
                row,
                accent_color=discord.Colour.blurple(),
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) == self.owner_id:
            return True
        await interaction.response.send_message(
            view=TerminalCardView(
                "🔒 Painel restrito",
                "Só o proprietário que abriu esta central pode usar estes botões.",
                color=discord.Colour.red(),
            ),
            ephemeral=True,
        )
        return False

    async def switch_page(self, interaction: discord.Interaction, page: str) -> None:
        if page not in {item[0] for item in self.PAGE_META}:
            await interaction.response.send_message(
                view=TerminalCardView("❌ Página inválida", "Essa página não existe.", color=discord.Colour.red()),
                ephemeral=True,
            )
            return
        self.page = page
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def on_timeout(self) -> None:
        for item in self.walk_children():
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


class TerminalFunRouter:
    def __init__(self, cog: Any):
        self.cog = cog
        self.bot = cog.bot
        self.handlers: dict[str, Callable[[commands.Context, str], Awaitable[None]]] = {
            "julgamento": self._cmd_julgamento,
            "oraculo": self._cmd_oraculo,
            "sorte": self._cmd_sorte,
            "batalha": self._cmd_batalha,
            "escolher": self._cmd_escolher,
            "moeda": self._cmd_moeda,
            "titulo": self._cmd_titulo,
            "raridade": self._cmd_raridade,
            "boleto": self._cmd_boleto,
            "cafe": self._cmd_cafe,
        }

    @property
    def subcommand_names(self) -> set[str]:
        return {"help", *self.handlers.keys()}

    async def handle(self, ctx: commands.Context, command: str) -> bool:
        parts = str(command or "").strip().split(maxsplit=1)
        if not parts:
            return False
        name = parts[0].casefold()
        args = parts[1].strip() if len(parts) > 1 else ""
        if name == "help":
            await self.show_help(ctx, args)
            return True
        handler = self.handlers.get(name)
        if handler is None:
            return False
        await handler(ctx, args)
        return True

    async def _reply(
        self,
        ctx: commands.Context,
        title: str,
        body: str,
        *,
        color: discord.Colour | int | None = None,
        target: Any = None,
        footer: str | None = None,
    ) -> discord.Message:
        return await ctx.reply(
            view=TerminalCardView(
                title,
                body,
                color=color,
                thumbnail_url=_avatar_url(target) if target is not None else None,
                thumbnail_description=f"Avatar de {_safe_display(getattr(target, 'display_name', target), 120)}" if target is not None else None,
                footer=footer,
            ),
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _error(self, ctx: commands.Context, title: str, body: str) -> None:
        await self._reply(ctx, title, body, color=discord.Colour.red())

    async def _resolve_user(self, ctx: commands.Context, raw: str, *, default_self: bool = True) -> Any | None:
        query = _clean_input(raw, 120)
        if not query:
            return ctx.author if default_self else None

        match = re.fullmatch(r"<@!?(\d+)>", query)
        user_id = int(match.group(1)) if match else int(query) if query.isdigit() else 0
        if user_id:
            guild = getattr(ctx, "guild", None)
            if guild is not None:
                member = guild.get_member(user_id)
                if member is not None:
                    return member
            user = self.bot.get_user(user_id)
            if user is not None:
                return user
            try:
                return await self.bot.fetch_user(user_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None

        guild = getattr(ctx, "guild", None)
        if guild is None:
            return None
        folded = query.casefold()
        exact: list[Any] = []
        for member in getattr(guild, "members", []):
            labels = {
                str(getattr(member, "display_name", "")).casefold(),
                str(getattr(member, "name", "")).casefold(),
                str(getattr(member, "global_name", "") or "").casefold(),
            }
            if folded in labels:
                exact.append(member)
        return exact[0] if len(exact) == 1 else None

    async def _target_or_error(self, ctx: commands.Context, raw: str, command: str) -> Any | None:
        target = await self._resolve_user(ctx, raw, default_self=True)
        if target is not None:
            return target
        await self._error(
            ctx,
            "👤 Membro não encontrado",
            f"Não consegui localizar `{_safe_display(raw, 120)}`.\n\nUse `{SPEC_BY_NAME[command].usage}`.",
        )
        return None

    def _target_name(self, target: Any) -> str:
        return _safe_display(
            getattr(target, "display_name", None)
            or getattr(target, "global_name", None)
            or getattr(target, "name", None)
            or str(target),
            120,
        )

    def help_page_content(self, page: str) -> str:
        if page == "tools":
            lines = [
                "## 🛠️ Ferramentas do proprietário",
                "Subcomandos para mensagens e manutenção do bot.\n",
            ]
            for spec in TOOL_SPECS:
                alias = f" · aliases: `{', '.join(spec.aliases)}`" if spec.aliases else ""
                lines.append(f"**`{spec.name}`**{alias}\n{spec.summary}\n`{spec.usage}`")
            return "\n\n".join(lines)[:3900]
        if page == "fun":
            lines = [
                "## 🎭 Diversão",
                "Dez comandos curtos, com humor brasileiro e referências indie usadas com moderação.\n",
            ]
            for spec in FUN_SPECS:
                lines.append(f"**`{spec.name}`** — {spec.summary}\n`{spec.usage}`")
            return "\n\n".join(lines)[:3900]
        if page == "usage":
            return (
                "## 📖 Como usar\n"
                "O primeiro termo depois de `_cmd` é tratado como subcomando quando estiver nesta central. "
                "Qualquer outro texto continua sendo executado pelo Bash.\n\n"
                "**Ajuda detalhada**\n"
                "`_cmd help moeda`\n"
                "`_cmd help canal`\n\n"
                "**Membros**\n"
                "Aceita ID, menção ou nome exato. Quando o membro é opcional, o alvo padrão é você.\n\n"
                "**Listas**\n"
                "Use `|` para separar opções ou participantes.\n"
                "`_cmd escolher pizza | hambúrguer | pastel`\n"
                "`_cmd batalha @Core | @João`\n\n"
                "**Terminal normal**\n"
                "`_cmd ls -la`\n"
                "`_cmd systemctl status tts-bot`"
            )
        return (
            "## 🧰 Central de comandos\n"
            "Ferramentas exclusivas do proprietário do bot.\n\n"
            "**Ferramentas**\n"
            "`help` · `dm` · `canal` · `nano`\n\n"
            "**Diversão**\n"
            "`julgamento` · `oraculo` · `sorte` · `batalha` · `escolher`\n"
            "`moeda` · `titulo` · `raridade` · `boleto` · `cafe`\n\n"
            "Use `_cmd help <subcomando>` para ver sintaxe e exemplo.\n"
            "Comandos que não aparecem nesta central continuam indo para o terminal normal."
        )

    async def show_help(self, ctx: commands.Context, raw_name: str = "") -> None:
        name = _clean_input(raw_name, 80).casefold()
        if name:
            spec = SPEC_BY_NAME.get(name)
            if spec is None:
                await self._error(
                    ctx,
                    "❓ Subcomando não encontrado",
                    f"Não existe um subcomando chamado `{_safe_display(name, 80)}`.\n\nUse `_cmd help` para abrir a lista completa.",
                )
                return
            alias_line = f"\n**Aliases**\n`{'`, `'.join(spec.aliases)}`" if spec.aliases else ""
            body = (
                f"{spec.summary}\n\n"
                f"**Uso**\n`{spec.usage}`\n\n"
                f"**Exemplo**\n`{spec.example}`"
                f"{alias_line}"
            )
            await self._reply(
                ctx,
                f"📖 {spec.name}",
                body,
                color=discord.Colour.blurple(),
                footer=f"Categoria: {spec.category}",
            )
            return

        view = TerminalHelpView(self, int(getattr(ctx.author, "id", 0) or 0))
        message = await ctx.reply(
            view=view,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        view.message = message

    async def _cmd_julgamento(self, ctx: commands.Context, args: str) -> None:
        target = await self._target_or_error(ctx, args, "julgamento")
        if target is None:
            return
        name = self._target_name(target)
        title, accusation, sentence = SYSTEM_RANDOM.choice(JUDGEMENTS)
        body = (
            f"**{name}** foi considerado culpado porque {accusation}.\n\n"
            f"**Sentença**\n{sentence}."
        )
        await self._reply(ctx, f"⚖️ {title}", body, color=discord.Colour.red(), target=target)

    async def _cmd_oraculo(self, ctx: commands.Context, args: str) -> None:
        question = _clean_input(args, 300)
        if not question:
            await self._error(ctx, "🔮 O oráculo aguarda", f"Faça uma pergunta.\n\nUse `{SPEC_BY_NAME['oraculo'].usage}`.")
            return
        heading, answer, closing = SYSTEM_RANDOM.choice(ORACLE_RESULTS)
        body = (
            f"**Pergunta**\n{_safe_display(question, 300)}\n\n"
            f"**Resposta**\n{answer}\n\n{closing}"
        )
        await self._reply(ctx, f"🔮 {heading}", body, color=discord.Colour.purple())

    async def _cmd_sorte(self, ctx: commands.Context, args: str) -> None:
        target = await self._target_or_error(ctx, args, "sorte")
        if target is None:
            return
        key = str(getattr(target, "id", self._target_name(target)))
        rng = _daily_rng("sorte", key)
        score = rng.randint(7, 99)
        if score < 20:
            rank = "D — Duvidosa"
        elif score < 40:
            rank = "C — Cautelosa"
        elif score < 60:
            rank = "B — Boa"
        elif score < 78:
            rank = "A — Abençoada"
        elif score < 90:
            rank = "S — Sortuda"
        elif score < 97:
            rank = "SS — Suspeitamente sortuda"
        else:
            rank = "SSS — Sai de casa agora"
        bonuses = rng.sample(list(LUCK_BONUSES), k=3)
        penalty = rng.choice(LUCK_PENALTIES)
        body = (
            f"**{self._target_name(target)}**\n"
            f"`{_progress_bar(score)}` **{score}%**\n"
            f"**Rank:** {rank}\n\n"
            "**Bônus ativos**\n"
            + "\n".join(f"+ {bonus}" for bonus in bonuses)
            + f"\n\n**Penalidade inevitável**\n- {penalty}."
        )
        await self._reply(
            ctx,
            "🍀 Sorte do dia",
            body,
            color=discord.Colour.green(),
            target=target,
            footer="O resultado permanece igual durante o dia.",
        )

    async def _cmd_batalha(self, ctx: commands.Context, args: str) -> None:
        raw = _clean_input(args, FUN_INPUT_MAX_CHARS)
        parts = [part.strip() for part in raw.split("|")]
        if len(parts) != 2 or not all(parts):
            await self._error(ctx, "🥊 Participantes inválidos", f"Separe dois membros com `|`.\n\nUse `{SPEC_BY_NAME['batalha'].usage}`.")
            return
        first = await self._resolve_user(ctx, parts[0], default_self=False)
        second = await self._resolve_user(ctx, parts[1], default_self=False)
        if first is None or second is None:
            missing = parts[0] if first is None else parts[1]
            await self._error(ctx, "👤 Membro não encontrado", f"Não consegui localizar `{_safe_display(missing, 120)}`.")
            return
        if int(getattr(first, "id", 0) or 0) == int(getattr(second, "id", 0) or 0):
            await self._error(ctx, "🥊 Batalha cancelada", "Escolha dois membros diferentes. Lutar contra o próprio reflexo fica para outro comando.")
            return
        winner, loser = (first, second) if SYSTEM_RANDOM.randrange(2) == 0 else (second, first)
        banner, scene, closing = SYSTEM_RANDOM.choice(BATTLE_SCENES)
        scene = scene.format(winner=self._target_name(winner), loser=self._target_name(loser))
        closing = closing.format(winner=self._target_name(winner), loser=self._target_name(loser))
        body = f"{scene}\n\n**Vencedor**\n{self._target_name(winner)}\n\n{closing}"
        await self._reply(ctx, f"🥊 {banner}", body, color=discord.Colour.dark_red(), target=winner)

    async def _cmd_escolher(self, ctx: commands.Context, args: str) -> None:
        raw = _clean_input(args, FUN_INPUT_MAX_CHARS)
        options = [_clean_input(part, FUN_OPTION_MAX_CHARS) for part in raw.split("|")]
        options = [option for option in options if option]
        if len(options) < 2:
            await self._error(ctx, "🎯 Faltam opções", f"Informe pelo menos duas opções separadas por `|`.\n\nUse `{SPEC_BY_NAME['escolher'].usage}`.")
            return
        if len(options) > FUN_OPTION_MAX_COUNT:
            await self._error(ctx, "🎯 Opções demais", f"Use no máximo {FUN_OPTION_MAX_COUNT} opções por vez.")
            return
        choice = SYSTEM_RANDOM.choice(options)
        heading, consequence = SYSTEM_RANDOM.choice(CHOICE_CONSEQUENCES)
        body = (
            f"**Opção selecionada**\n{_safe_display(choice, FUN_OPTION_MAX_CHARS)}\n\n"
            f"**{heading}**\n{consequence}"
        )
        await self._reply(ctx, "🎯 Escolha registrada", body, color=discord.Colour.blurple())

    async def _cmd_moeda(self, ctx: commands.Context, args: str) -> None:
        if _clean_input(args, 30):
            await self._error(ctx, "🪙 Uso da moeda", "Este subcomando não precisa de argumentos.\n\nUse `_cmd moeda`.")
            return
        roll = SYSTEM_RANDOM.random()
        if roll < 0.025:
            body = (
                "**Resultado**\nA moeda caiu em pé.\n\n"
                "O destino se recusou a escolher e agora exige replay em câmera lenta.\n\n"
                "**Rank:** SSS — Suspeito"
            )
            title = "🪙 Evento raro"
        elif roll < 0.5125:
            variants = (
                ("+ULTRARICOSHOT", "A moeda ricocheteou em três decisões sensatas e acertou justamente a conta mais cara do mês."),
                ("+PARRY", "Você rebateu a responsabilidade. Ela volta no próximo turno com juros."),
                ("Despite everything, it’s still you.", "Foi você que pediu cara e agora precisa respeitar o resultado."),
            )
            heading, line = SYSTEM_RANDOM.choice(variants)
            body = f"**Resultado**\nCara\n\n**{heading}**\n{line}"
            title = "🪙 Moeda lançada"
        else:
            variants = (
                ("+RICOSHOT", "A moeda acertou o único lado que alguém jurou que não escolheria."),
                ("There is no escape.", "Deu coroa. A tarefa continua sendo sua."),
                ("A brawl is surely brewing!", "Deu coroa e alguém já começou a discutir se o lançamento valeu."),
            )
            heading, line = SYSTEM_RANDOM.choice(variants)
            body = f"**Resultado**\nCoroa\n\n**{heading}**\n{line}"
            title = "🪙 Moeda lançada"
        await self._reply(ctx, title, body, color=discord.Colour.gold())

    async def _cmd_titulo(self, ctx: commands.Context, args: str) -> None:
        target = await self._target_or_error(ctx, args, "titulo")
        if target is None:
            return
        key = str(getattr(target, "id", self._target_name(target)))
        rng = _daily_rng("titulo", key)
        title, skill, downside = rng.choice(TITLES)
        body = (
            f"**{self._target_name(target)} agora é:**\n### {title}\n\n"
            f"**Habilidade**\n{skill}.\n\n"
            f"**Desvantagem**\n{downside}."
        )
        await self._reply(
            ctx,
            "👑 Novo título desbloqueado",
            body,
            color=discord.Colour.gold(),
            target=target,
            footer="O título permanece igual durante o dia.",
        )

    async def _cmd_raridade(self, ctx: commands.Context, args: str) -> None:
        raw = _clean_input(args, 180)
        if not raw:
            await self._error(ctx, "✨ Item ausente", f"Informe uma pessoa ou coisa para analisar.\n\nUse `{SPEC_BY_NAME['raridade'].usage}`.")
            return

        target = await self._resolve_user(ctx, raw, default_self=False)
        if target is None and _looks_like_user_reference(raw):
            await self._error(ctx, "👤 Membro não encontrado", f"Não consegui localizar `{_safe_display(raw, 120)}`.")
            return
        subject = self._target_name(target) if target is not None else _safe_display(raw, 180)
        key = str(getattr(target, "id", raw))
        rng = _daily_rng("raridade", key)
        item, rarity, effect, stats = rng.choice(RARITY_ITEMS)
        body = (
            f"**Alvo analisado**\n{subject}\n\n"
            f"### {item}\n"
            f"**Raridade:** {rarity}\n\n"
            f"**Efeito**\n{effect}.\n\n"
            f"**Atributos**\n{stats}"
        )
        await self._reply(
            ctx,
            "✨ Item identificado",
            body,
            color=discord.Colour.purple(),
            target=target,
            footer="A identificação permanece igual durante o dia.",
        )

    async def _cmd_boleto(self, ctx: commands.Context, args: str) -> None:
        target = await self._target_or_error(ctx, args, "boleto")
        if target is None:
            return
        key = str(getattr(target, "id", self._target_name(target)))
        rng = _daily_rng("boleto", key)
        reason, amount, note = rng.choice(BILLS)
        body = (
            f"**Responsável pela cobrança**\n{self._target_name(target)}\n\n"
            f"**Motivo**\n{reason}.\n\n"
            f"**Valor cenográfico**\n### {amount}\n\n"
            f"{note}"
        )
        await self._reply(
            ctx,
            "🧾 Boleto fictício emitido",
            body,
            color=discord.Colour.orange(),
            target=target,
            footer="Sem valor real, código de barras ou qualquer validade financeira.",
        )

    async def _cmd_cafe(self, ctx: commands.Context, args: str) -> None:
        target = await self._target_or_error(ctx, args, "cafe")
        if target is None:
            return
        key = str(getattr(target, "id", self._target_name(target)))
        rng = _daily_rng("cafe", key)
        score = rng.randint(4, 100)
        heading = ""
        description = ""
        tasks: tuple[str, ...] = ()
        for score_range, candidate_heading, candidate_description, candidate_tasks in COFFEE_STATES:
            if score in score_range:
                heading = candidate_heading
                description = candidate_description
                tasks = candidate_tasks
                break
        selected_tasks = rng.sample(list(tasks), k=min(2, len(tasks)))
        body = (
            f"**{self._target_name(target)}**\n"
            f"`{_progress_bar(score)}` **{score}%**\n\n"
            f"**{heading}**\n{description}\n\n"
            "**Capacidade atual**\n"
            + "\n".join(f"+ {task}" for task in selected_tasks)
        )
        await self._reply(
            ctx,
            "☕ Nível de café",
            body,
            color=discord.Colour.from_rgb(166, 104, 52),
            target=target,
            footer="O nível permanece igual durante o dia.",
        )
