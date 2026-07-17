import { expect, test, type Page } from "@playwright/test";

type Viewport = { width: number; height: number };

async function openAssistant(
  page: Page,
  viewport: Viewport,
  options: {
    conversationId?: number;
    expectComposer?: boolean;
    theme?: "light" | "dark";
  } = {},
) {
  const {
    conversationId = 1,
    expectComposer = true,
    theme = "light",
  } = options;
  await page.setViewportSize(viewport);
  await page.addInitScript(
    ({ selectedTheme }) => {
      window.localStorage.setItem("anacleto:onboarding:v1:1", "seen");
      window.localStorage.setItem("anacleto:sidebar:v1", "expanded");
      window.localStorage.setItem("theme", selectedTheme);
    },
    { selectedTheme: theme },
  );
  await page.goto(`/asistente?c=${conversationId}`);
  if (expectComposer) {
    await expect(page.locator(".assistant-thread")).toBeVisible();
    await expect(page.getByLabel("Mensaje para Anacleto")).toBeVisible();
  }
  if (conversationId === 1) {
    await expect(page.getByText("Comparativa de artículos")).toBeVisible();
  }
}

async function documentOverflow(page: Page) {
  return page.evaluate(() => ({
    clientHeight: document.documentElement.clientHeight,
    clientWidth: document.documentElement.clientWidth,
    scrollHeight: document.documentElement.scrollHeight,
    scrollWidth: document.documentElement.scrollWidth,
  }));
}

async function textContrastRatio(
  page: Page,
  foregroundSelector: string,
  backgroundSelector: string,
  pseudoElement?: string,
) {
  return page.evaluate(
    ({ background, foreground, pseudo }) => {
      const foregroundElement = document.querySelector(foreground);
      const backgroundElement = document.querySelector(background);
      if (!foregroundElement || !backgroundElement) {
        throw new Error(`Missing contrast fixture: ${foreground} / ${background}`);
      }

      const parseColor = (value: string) => {
        const channels = value.match(/[\d.]+/g)?.slice(0, 3).map(Number);
        if (!channels || channels.length !== 3) {
          throw new Error(`Unsupported computed color: ${value}`);
        }
        return channels.map((channel) => {
          const normalized = channel / 255;
          return normalized <= 0.04045
            ? normalized / 12.92
            : ((normalized + 0.055) / 1.055) ** 2.4;
        });
      };
      const luminance = (value: string) => {
        const [red, green, blue] = parseColor(value);
        return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
      };
      const foregroundLuminance = luminance(
        getComputedStyle(foregroundElement, pseudo || null).color,
      );
      const backgroundLuminance = luminance(
        getComputedStyle(backgroundElement).backgroundColor,
      );
      return (
        (Math.max(foregroundLuminance, backgroundLuminance) + 0.05) /
        (Math.min(foregroundLuminance, backgroundLuminance) + 0.05)
      );
    },
    {
      background: backgroundSelector,
      foreground: foregroundSelector,
      pseudo: pseudoElement,
    },
  );
}

const requiredViewports: Viewport[] = [
  { width: 1920, height: 950 },
  { width: 1440, height: 900 },
  { width: 1366, height: 768 },
  { width: 1024, height: 768 },
  { width: 900, height: 768 },
  { width: 768, height: 844 },
  { width: 390, height: 844 },
];

for (const viewport of requiredViewports) {
  test(`mantiene chat y compositor dentro de ${viewport.width}x${viewport.height}`, async ({
    page,
  }) => {
    await openAssistant(page, viewport);

    const messages = page.locator(".assistant-messages");
    const composerStack = page.locator(".assistant-composer-stack");
    const thread = page.locator(".assistant-thread");
    const [messagesBox, composerBox, threadBox] = await Promise.all([
      messages.boundingBox(),
      composerStack.boundingBox(),
      thread.boundingBox(),
    ]);

    expect(messagesBox).not.toBeNull();
    expect(composerBox).not.toBeNull();
    expect(threadBox).not.toBeNull();
    expect(messagesBox!.y + messagesBox!.height).toBeLessThanOrEqual(
      composerBox!.y + 1,
    );
    expect(composerBox!.y + composerBox!.height).toBeLessThanOrEqual(
      viewport.height + 1,
    );
    expect(threadBox!.y + threadBox!.height).toBeCloseTo(viewport.height, 0);

    const dimensions = await documentOverflow(page);
    expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.clientWidth + 1);
    expect(dimensions.scrollHeight).toBeLessThanOrEqual(dimensions.clientHeight + 1);
    expect(
      await messages.evaluate((element) => element.scrollHeight > element.clientHeight),
    ).toBe(true);
  });
}

test("encapsula una tabla Markdown ancha sin perder semántica", async ({ page }) => {
  await openAssistant(page, { width: 390, height: 844 });

  const wrapper = page.getByRole("region", { name: "Tabla de la respuesta" });
  const table = wrapper.locator("table");
  const bubble = wrapper.locator("xpath=ancestor::*[contains(@class,'assistant-message-bubble')]");
  await expect(table).toBeVisible();
  await expect(table.locator("thead")).toHaveCount(1);
  await expect(table.locator("th")).toHaveCount(8);
  expect(
    await table
      .locator("th")
      .first()
      .evaluate((element) => getComputedStyle(element).textTransform),
  ).toBe("none");

  const [wrapperBox, bubbleBox] = await Promise.all([
    wrapper.boundingBox(),
    bubble.boundingBox(),
  ]);
  expect(wrapperBox).not.toBeNull();
  expect(bubbleBox).not.toBeNull();
  expect(wrapperBox!.x).toBeGreaterThanOrEqual(bubbleBox!.x - 1);
  expect(wrapperBox!.x + wrapperBox!.width).toBeLessThanOrEqual(
    bubbleBox!.x + bubbleBox!.width + 1,
  );
  expect(
    await wrapper.evaluate((element) => element.scrollWidth > element.clientWidth),
  ).toBe(true);

  await wrapper.evaluate((element) => {
    element.scrollLeft = element.scrollWidth;
  });
  expect(await wrapper.evaluate((element) => element.scrollLeft)).toBeGreaterThan(0);
  const dimensions = await documentOverflow(page);
  expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.clientWidth + 1);
});

test("inicia el compositor en una línea, crece hasta 180 px y se reinicia", async ({
  page,
}) => {
  await openAssistant(page, { width: 768, height: 844 }, { conversationId: 2 });
  const textarea = page.getByLabel("Mensaje para Anacleto");
  const initialHeight = await textarea.evaluate((element) => element.clientHeight);
  expect(initialHeight).toBeLessThan(50);

  await textarea.fill(
    "Este borrador intermedio debe reajustar su altura cuando cambia el ancho disponible, incluso si la usuaria no vuelve a escribir después de redimensionar la ventana.",
  );
  const wideDraftHeight = await textarea.evaluate(
    (element) => element.clientHeight,
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(() => textarea.evaluate((element) => element.clientHeight))
    .toBeGreaterThan(wideDraftHeight);
  await page.setViewportSize({ width: 768, height: 844 });

  const longDraft = Array.from(
    { length: 24 },
    (_, index) => `Línea ${index + 1} para comprobar el crecimiento automático.`,
  ).join("\n");
  await textarea.fill(longDraft);
  const grown = await textarea.evaluate((element) => ({
    clientHeight: element.clientHeight,
    overflowY: getComputedStyle(element).overflowY,
    scrollHeight: element.scrollHeight,
  }));
  expect(grown.clientHeight).toBeLessThanOrEqual(180);
  expect(grown.clientHeight).toBeGreaterThan(initialHeight);
  expect(grown.scrollHeight).toBeGreaterThan(grown.clientHeight);
  expect(grown.overflowY).toBe("auto");

  await textarea.fill("");
  await expect
    .poll(() => textarea.evaluate((element) => element.clientHeight))
    .toBeLessThan(50);

  const suggestion = page.getByRole("button", {
    name: "Preparar un resumen ejecutivo",
  });
  await expect(suggestion).toBeVisible();
  await suggestion.click();
  await expect(textarea).toHaveValue("Preparar un resumen ejecutivo");

  await textarea.fill("Enviar una prueba breve");
  await page.getByRole("button", { name: "Enviar mensaje" }).click();
  await expect(textarea).toHaveValue("");
  await expect
    .poll(() => textarea.evaluate((element) => element.clientHeight))
    .toBeLessThan(50);
});

test("no muestra sugerencias tras iniciar la conversación y las mantiene en una fila móvil", async ({
  page,
}) => {
  await openAssistant(page, { width: 390, height: 844 });
  await expect(page.locator(".assistant-chip")).toHaveCount(0);

  await page.goto("/asistente?c=2");
  await expect(page.locator(".assistant-chip")).toHaveCount(3);
  const chipPositions = await page.locator(".assistant-chip").evaluateAll((chips) =>
    chips.map((chip) => Math.round(chip.getBoundingClientRect().top)),
  );
  expect(new Set(chipPositions).size).toBe(1);
  expect(
    await page.locator(".assistant-chips").evaluate(
      (element) => element.scrollWidth >= element.clientWidth,
    ),
  ).toBe(true);
});

test("preserva la lectura durante streaming y ofrece volver al final", async ({ page }) => {
  await openAssistant(page, { width: 1366, height: 768 });
  const messages = page.locator(".assistant-messages");
  const textarea = page.getByLabel("Mensaje para Anacleto");

  await textarea.fill("Incorpora una disposición transitoria.");
  await page.getByRole("button", { name: "Enviar mensaje" }).click();
  await expect(page.getByRole("button", { name: "Detener generación" })).toBeVisible();
  await messages.evaluate((element) => {
    element.scrollTop = 0;
    element.dispatchEvent(new Event("scroll", { bubbles: true }));
  });
  const readingPosition = await messages.evaluate((element) => element.scrollTop);

  await expect(page.getByText("He incorporado tu indicación", { exact: false })).toBeVisible();
  await expect(page.getByRole("button", { name: "Ir a la última respuesta" })).toBeVisible();
  expect(await messages.evaluate((element) => element.scrollTop)).toBeCloseTo(
    readingPosition,
    0,
  );

  await page.getByRole("button", { name: "Ir a la última respuesta" }).click();
  await expect(
    page.getByRole("button", { name: "Detener generación" }),
  ).toHaveCount(0);
  await expect
    .poll(() =>
      messages.evaluate(
        (element) =>
          element.scrollHeight - element.scrollTop - element.clientHeight,
      ),
    )
    .toBeLessThanOrEqual(2);
});

for (const width of [900, 768, 390]) {
  test(`abre y gestiona por teclado el drawer de conversaciones a ${width} px`, async ({
    page,
  }) => {
    await openAssistant(page, { width, height: 844 });
    const drawer = page.getByRole("complementary", { name: "Conversaciones" });
    const openButton = page.getByRole("button", {
      name: "Desplegar lista de chats",
    });
    const closeButton = page.getByRole("button", {
      name: "Plegar lista de chats",
    });
    await expect(drawer).toHaveCount(0);

    await openButton.click();
    await expect(drawer).toBeVisible();
    await expect(closeButton).toBeFocused();
    await expect(
      page.getByRole("button", { name: "Cerrar lista de conversaciones" }),
    ).toHaveAttribute("tabindex", "-1");
    expect(
      await page
        .locator(".assistant-thread")
        .evaluate((element) => (element as HTMLElement).inert),
    ).toBe(true);
    const drawerBox = await drawer.boundingBox();
    expect(drawerBox).not.toBeNull();
    expect(drawerBox!.width).toBeLessThan(width);

    await page.keyboard.press("Escape");
    await expect(drawer).toHaveCount(0);
    await expect(openButton).toBeFocused();
    const dimensions = await documentOverflow(page);
    expect(dimensions.scrollWidth).toBeLessThanOrEqual(
      dimensions.clientWidth + 1,
    );
  });
}

test("mantiene adjuntos y controles del compositor utilizables en 390 px", async ({
  page,
}) => {
  await openAssistant(page, { width: 390, height: 844 });
  await page.getByRole("button", { name: "Adjuntar archivo" }).click();
  await expect(page.getByText("memoria-explicativa.pdf").last()).toBeVisible();

  for (const control of [
    page.getByRole("button", { name: "Adjuntar archivo" }),
    page.getByRole("button", { name: "Activar modo voz" }),
    page.getByRole("button", { name: "Grabar audio" }),
    page.getByRole("button", { name: "Enviar mensaje" }),
  ]) {
    const box = await control.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(390);
  }
  const dimensions = await documentOverflow(page);
  expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.clientWidth + 1);
});

test("muestra el lienzo a ancho completo sin recorte en el umbral de 901 px", async ({
  page,
}) => {
  await openAssistant(
    page,
    { width: 901, height: 768 },
    { conversationId: 3, expectComposer: false },
  );
  const canvas = page.getByRole("complementary", {
    name: "Lienzo documental",
  });
  await expect(canvas).toBeVisible();
  await expect(page.locator("#canvas-document-title")).toHaveValue(
    "Ordenanza de convivencia",
  );

  await expect(page.locator(".assistant-thread")).toBeHidden();
  const canvasBox = await canvas.boundingBox();
  expect(canvasBox).not.toBeNull();
  expect(canvasBox!.width).toBeGreaterThanOrEqual(660);
  expect(canvasBox!.x + canvasBox!.width).toBeLessThanOrEqual(902);
  const dimensions = await documentOverflow(page);
  expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.clientWidth + 1);
});

for (const theme of ["light", "dark"] as const) {
  test(`cumple contraste de texto y conserva layout en tema ${theme}`, async ({
    page,
  }) => {
    await openAssistant(page, { width: 1440, height: 900 }, { theme });
    await expect(page.locator("html")).toHaveClass(
      theme === "dark" ? /dark/ : /^(?!.*dark)/,
    );

    for (const [foreground, background, pseudo] of [
      [".assistant-message-meta", ".assistant-thread", undefined],
      [".assistant-composer textarea", ".assistant-composer", "::placeholder"],
      [
        ".assistant-message-attachment-copy small",
        ".assistant-message-attachment",
        undefined,
      ],
      [
        ".assistant-markdown-table-scroll th",
        ".assistant-markdown-table-scroll th",
        undefined,
      ],
      [
        ".assistant-message.assistant .assistant-message-avatar",
        ".assistant-message.assistant .assistant-message-avatar",
        undefined,
      ],
    ] as const) {
      expect(
        await textContrastRatio(page, foreground, background, pseudo),
      ).toBeGreaterThanOrEqual(4.5);
    }

    await page.goto("/asistente?c=2");
    await expect(page.locator(".assistant-chip")).toHaveCount(3);
    expect(
      await textContrastRatio(page, ".assistant-chip", ".assistant-chip"),
    ).toBeGreaterThanOrEqual(4.5);

    const dimensions = await documentOverflow(page);
    expect(dimensions.scrollWidth).toBeLessThanOrEqual(
      dimensions.clientWidth + 1,
    );
  });
}

for (const zoom of [1.25, 2]) {
  test(`mantiene el layout en viewport equivalente a zoom ${zoom * 100}%`, async ({
    page,
  }) => {
    const viewport = {
      width: Math.round(1440 / zoom),
      height: Math.round(900 / zoom),
    };
    await openAssistant(page, viewport);
    const dimensions = await documentOverflow(page);
    expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.clientWidth + 1);
    const composerBox = await page.locator(".assistant-composer-stack").boundingBox();
    expect(composerBox).not.toBeNull();
    expect(composerBox!.y + composerBox!.height).toBeLessThanOrEqual(
      viewport.height + 1,
    );
  });
}
