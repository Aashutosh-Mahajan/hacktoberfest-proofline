import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { SimulatePopover, TopBar } from "./Bars";
import { api } from "../net/api";
import { initialState } from "../store/reducer";
import { useStore } from "../store/store";

beforeEach(() => {
  useStore.setState(initialState());
  vi.spyOn(api, "replayList").mockResolvedValue({ ok: true, runs: [], active: null });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("dashboard scenario controls", () => {
  it("offers all seven scenarios and prevents start times rejected by the backend", () => {
    const scenario = vi.spyOn(api, "scenario").mockResolvedValue({ ok: true });
    render(<SimulatePopover onClose={vi.fn()} />);
    const choices = screen.getByRole("combobox", { name: "What should go wrong?" });
    expect(within(choices).getAllByRole("option").map((option) => option.textContent)).toEqual([
      "Bad software update", "Bank outage", "Bank outage with a misleading update",
      "Customers hitting limits", "Traffic surge", "Two problems at once", "Everything normal",
    ]);
    fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "0" } });
    expect(screen.getByRole("button", { name: "Start failure" })).toBeDisabled();
    expect(screen.getByText(/between 5 and 120 seconds/)).toBeInTheDocument();
    expect(scenario).not.toHaveBeenCalled();
  });

  it("keeps the controls open on an API error and closes only after a successful start", async () => {
    const scenario = vi.spyOn(api, "scenario").mockResolvedValue({ ok: false, error: "backend unavailable" });
    const onClose = vi.fn();
    render(<SimulatePopover onClose={onClose} />);
    fireEvent.click(screen.getByRole("button", { name: "Start failure" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Start failure" })).toBeEnabled());
    expect(onClose).not.toHaveBeenCalled();
    scenario.mockResolvedValue({ ok: true });
    fireEvent.click(screen.getByRole("button", { name: "Start failure" }));
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
  });

  it("shows only the retained navigation tabs", () => {
    render(<TopBar view="live" setView={vi.fn()} simOpen={false} toggleSim={vi.fn()} />);
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["Live", "Banks", "Ask"]);
  });
});
