import http from "node:http";

const port = Number(process.env.MOCK_ASSISTANT_API_PORT ?? 3101);
const frontendOrigin = process.env.MOCK_FRONTEND_ORIGIN ?? "http://127.0.0.1:3100";
const now = "2026-07-17T10:00:00Z";

const user = {
  id: 1,
  email: "secretaria@example.test",
  full_name: "Secretaría Municipal",
  is_active: true,
  is_superuser: false,
  permissions: ["assistant.use"],
  organizations: [
    {
      id: 1,
      name: "Ayuntamiento de Valdemora",
      status: "active",
      municipality_id: 1,
      municipality: {
        id: 1,
        name: "Valdemora",
        province: "Madrid",
        autonomous_community: "Comunidad de Madrid",
      },
    },
  ],
  groups: [],
};

const longParagraph =
  "Este texto de prueba representa una respuesta extensa del asistente municipal. " +
  "Incluye antecedentes, criterios de redacción, observaciones de procedimiento y próximos pasos para comprobar que todo el contenido puede desplazarse por encima del compositor sin quedar oculto.";

const wideTable = `### Comparativa de artículos

| Artículo | Finalidad | Órgano competente | Plazo | Documentación | Publicación | Seguimiento | Observaciones |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Regular la convivencia en espacios públicos municipales | Alcaldía y Secretaría General | 20 días hábiles | Memoria, informe jurídico y memoria económica | Portal de transparencia y boletín oficial | Revisión anual por la comisión informativa | IdentificadorExtensoSinSeparadoresParaComprobarElAjusteDentroDeLaCelda |
| 2 | Establecer el procedimiento de autorización | Junta de Gobierno Local | 10 días hábiles | Solicitud, plano y declaración responsable | Tablón electrónico municipal | Informe semestral de cumplimiento | Debe coordinarse con las áreas de urbanismo y medio ambiente |
| 3 | Definir el régimen de inspección y control | Concejalía delegada | 15 días hábiles | Acta de inspección y propuesta de resolución | Notificación individual | Registro de actuaciones | Se preservará la audiencia de las personas interesadas |

La tabla anterior conserva semántica HTML y debe desplazarse solo dentro de la respuesta.`;

const codeExample = `### Ejemplo de estructura

\`\`\`text
EXPOSICIÓN DE MOTIVOS
Artículo 1. Objeto.
Artículo 2. Ámbito de aplicación.
Disposición final. Entrada en vigor.
\`\`\``;

function message(id, role, content, extra = {}) {
  return {
    id,
    role,
    content,
    actions: [],
    attachments: [],
    agent_key: role === "assistant" ? "anacleto" : null,
    routing: null,
    created_at: now,
    ...extra,
  };
}

const richMessages = [
  message(1, "user", "Necesito revisar un borrador de ordenanza municipal."),
  message(2, "assistant", `${longParagraph}\n\n${longParagraph}`),
  message(3, "user", "Incluye una comparación clara de los artículos."),
  message(4, "assistant", wideTable),
  message(5, "user", "Añade también una estructura de referencia."),
  message(6, "assistant", `${codeExample}\n\n${longParagraph}`),
  message(7, "user", "Adjunto la memoria explicativa."),
  message(8, "assistant", `${longParagraph}\n\n${longParagraph}`, {
    attachments: [
      {
        id: 1,
        document_id: 50,
        project_id: 7,
        project_name: "Ordenanza de convivencia",
        filename: "memoria-explicativa.pdf",
        content_type: "application/pdf",
        size_bytes: 245760,
        context_status: "unsupported",
        context_char_count: 0,
      },
    ],
  }),
  message(9, "user", "Detalla los pasos que faltan antes de aprobarla."),
  message(
    10,
    "assistant",
    Array.from({ length: 8 }, (_, index) => `${index + 1}. ${longParagraph}`).join(
      "\n\n",
    ),
  ),
];

const conversations = [
  {
    id: 1,
    title: "Borrador de ordenanza municipal",
    status: "active",
    folder_id: null,
    created_at: now,
    updated_at: now,
  },
  {
    id: 2,
    title: "Nueva conversación",
    status: "active",
    folder_id: null,
    created_at: now,
    updated_at: now,
  },
  {
    id: 3,
    title: "Borrador abierto en lienzo",
    status: "active",
    folder_id: null,
    created_at: now,
    updated_at: now,
  },
];

const details = new Map([
  [1, { ...conversations[0], messages: richMessages }],
  [2, { ...conversations[1], messages: [] }],
  [3, { ...conversations[2], messages: richMessages.slice(0, 4) }],
]);

const canvasDocumentSummary = {
  id: 21,
  conversation_id: 3,
  organization_id: 1,
  document_type: "municipal_ordinance",
  title: "Ordenanza de convivencia",
  status: "draft",
  current_revision: 1,
  created_at: now,
  updated_at: now,
};
const canvasDocument = {
  ...canvasDocumentSummary,
  content: "# Ordenanza de convivencia\n\n## Artículo 1. Objeto\n\nTexto del borrador municipal.",
  content_format: "markdown",
};

const organization = user.organizations[0];
const project = {
  id: 7,
  name: "Ordenanza de convivencia",
  description: "Documentación de prueba",
  status: "active",
  organization_id: 1,
  organization,
  users: [],
  groups: [],
  created_at: now,
  updated_at: now,
};
const document = {
  id: 50,
  organization_id: 1,
  project_id: 7,
  original_filename: "memoria-explicativa.pdf",
  stored_filename: "memoria-explicativa.pdf",
  storage_backend: "local",
  storage_key: "e2e/memoria-explicativa.pdf",
  content_type: "application/pdf",
  size_bytes: 245760,
  checksum_sha256: "e2e",
  status: "active",
  uploaded_by_id: 1,
  created_at: now,
  updated_at: now,
};

function setCorsHeaders(response) {
  response.setHeader("Access-Control-Allow-Credentials", "true");
  response.setHeader("Access-Control-Allow-Headers", "Content-Type, Authorization");
  response.setHeader("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS");
  response.setHeader("Access-Control-Allow-Origin", frontendOrigin);
  response.setHeader("Vary", "Origin");
}

function sendJson(response, body, status = 200) {
  setCorsHeaders(response);
  response.writeHead(status, { "Content-Type": "application/json; charset=utf-8" });
  response.end(JSON.stringify(body));
}

async function readJson(request) {
  let raw = "";
  for await (const chunk of request) {
    raw += chunk;
  }
  return raw ? JSON.parse(raw) : {};
}

function sendSseFrame(response, event, data) {
  response.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
}

async function streamMessage(request, response, conversationId) {
  const body = await readJson(request);
  const detail = details.get(conversationId);
  if (!detail) {
    sendJson(response, { detail: "Conversation not found" }, 404);
    return;
  }

  const nextUserId = 100 + detail.messages.length;
  const nextAssistantId = nextUserId + 1;
  const userMessage = message(nextUserId, "user", String(body.content ?? ""));
  const assistantContent =
    "He incorporado tu indicación al borrador. Mantengo visibles los antecedentes y continúo con una respuesta en streaming sin alterar tu posición de lectura.";
  const assistantMessage = message(nextAssistantId, "assistant", assistantContent);

  setCorsHeaders(response);
  response.writeHead(200, {
    "Cache-Control": "no-cache, no-transform",
    Connection: "keep-alive",
    "Content-Type": "text/event-stream; charset=utf-8",
  });
  response.flushHeaders();
  sendSseFrame(response, "message_start", {
    conversation_id: conversationId,
    user_message_id: userMessage.id,
    user_message: userMessage,
  });

  await new Promise((resolve) => setTimeout(resolve, 700));
  const chunks = [
    "He incorporado tu indicación al borrador. ",
    "Mantengo visibles los antecedentes y continúo con una respuesta en streaming ",
    "sin alterar tu posición de lectura.",
  ];
  for (const text of chunks) {
    if (response.destroyed || response.writableEnded) {
      return;
    }
    sendSseFrame(response, "text_delta", { text });
    await new Promise((resolve) => setTimeout(resolve, 180));
  }

  const summary = conversations.find((conversation) => conversation.id === conversationId);
  const updatedConversation = {
    ...summary,
    updated_at: new Date().toISOString(),
  };
  sendSseFrame(response, "done", {
    message: assistantMessage,
    user_message: userMessage,
    conversation: updatedConversation,
  });
  response.end();
}

const server = http.createServer(async (request, response) => {
  const url = new URL(request.url ?? "/", `http://${request.headers.host}`);
  const path = url.pathname;

  if (request.method === "OPTIONS") {
    setCorsHeaders(response);
    response.writeHead(204);
    response.end();
    return;
  }

  if (path === "/health") {
    sendJson(response, { ok: true });
    return;
  }
  if (path === "/auth/me") {
    sendJson(response, user);
    return;
  }
  if (path === "/assistant/status") {
    sendJson(response, {
      enabled: true,
      runtime: "e2e",
      model: "mock",
      runtime_healthy: true,
      speech_transcription_enabled: true,
      speech_synthesis_enabled: true,
      speech_synthesis_max_chars: 3000,
      realtime_voice_enabled: false,
      realtime_voice_provider: null,
      realtime_voice_model: null,
      web_page_reader_enabled: false,
      realtime_web_page_reader_enabled: false,
      tools: [],
    });
    return;
  }
  if (path === "/assistant/conversation-folders") {
    sendJson(response, []);
    return;
  }
  if (path === "/assistant/conversations" && request.method === "GET") {
    sendJson(response, conversations);
    return;
  }
  if (path === "/assistant/conversations" && request.method === "POST") {
    sendJson(response, details.get(2), 201);
    return;
  }

  const detailMatch = path.match(/^\/assistant\/conversations\/(\d+)$/);
  if (detailMatch && request.method === "GET") {
    const detail = details.get(Number(detailMatch[1]));
    sendJson(response, detail ?? { detail: "Conversation not found" }, detail ? 200 : 404);
    return;
  }
  const canvasMatch = path.match(/^\/assistant\/conversations\/(\d+)\/canvas$/);
  if (canvasMatch && request.method === "GET") {
    const conversationId = Number(canvasMatch[1]);
    sendJson(
      response,
      conversationId === 3
        ? { documents: [canvasDocumentSummary], active_document_id: 21 }
        : { documents: [], active_document_id: null },
    );
    return;
  }
  if (path === "/assistant/canvas/documents/21" && request.method === "GET") {
    sendJson(response, canvasDocument);
    return;
  }
  const streamMatch = path.match(
    /^\/assistant\/conversations\/(\d+)\/messages\/stream$/,
  );
  if (streamMatch && request.method === "POST") {
    await streamMessage(request, response, Number(streamMatch[1]));
    return;
  }
  if (path === "/projects" && request.method === "GET") {
    sendJson(response, [project]);
    return;
  }
  if (path === "/projects/7/documents" && request.method === "GET") {
    sendJson(response, [document]);
    return;
  }

  sendJson(response, { detail: `Unhandled mock route: ${request.method} ${path}` }, 404);
});

server.listen(port, "127.0.0.1", () => {
  process.stdout.write(`Mock assistant API listening on http://127.0.0.1:${port}\n`);
});

function closeServer() {
  server.close(() => process.exit(0));
}

process.on("SIGINT", closeServer);
process.on("SIGTERM", closeServer);
