"use client";

import {
  createContext,
  useContext,
  type ReactNode,
} from "react";
import { useAssistantController } from "./useAssistantController";
import { useSession } from "./session";

type AssistantController = ReturnType<typeof useAssistantController>;

const AssistantControllerContext = createContext<AssistantController | null>(
  null,
);

function AssistantControllerState({ children }: { children: ReactNode }) {
  const { getStoredToken, handleRequestError } = useSession();
  const controller = useAssistantController({
    getStoredToken,
    handleRequestError,
  });

  return (
    <AssistantControllerContext.Provider value={controller}>
      {children}
    </AssistantControllerContext.Provider>
  );
}

export function AssistantControllerProvider({
  children,
}: {
  children: ReactNode;
}) {
  const { user } = useSession();

  return (
    <AssistantControllerState key={user?.id ?? "anonymous"}>
      {children}
    </AssistantControllerState>
  );
}

export function useAssistantControllerContext() {
  const controller = useContext(AssistantControllerContext);
  if (!controller) {
    throw new Error(
      "useAssistantControllerContext debe usarse dentro de <AssistantControllerProvider>.",
    );
  }
  return controller;
}
