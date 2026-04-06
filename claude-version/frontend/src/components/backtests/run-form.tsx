"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { FlaskConical, Play } from "lucide-react";
import { useAuth } from "@/lib/auth";
import { apiFetch } from "@/lib/api";
import { strategies } from "@/lib/mock-data/strategies";

interface RunBacktestFormProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function RunBacktestForm({
  open,
  onOpenChange,
}: RunBacktestFormProps): React.ReactElement {
  const { getToken } = useAuth();
  const [selectedStrategy, setSelectedStrategy] = useState("");
  const [instruments, setInstruments] = useState("");
  const [startDate, setStartDate] = useState("2025-01-01");
  const [endDate, setEndDate] = useState("2025-12-31");
  const [config, setConfig] = useState("{}");

  const handleRunBacktest = async (): Promise<void> => {
    try {
      const token = await getToken();
      await apiFetch(
        "/api/v1/backtests/run",
        {
          method: "POST",
          body: JSON.stringify({
            strategy_id: selectedStrategy,
            config: JSON.parse(config),
            instruments: instruments
              .split(",")
              .map((s) => s.trim())
              .filter(Boolean),
            start_date: startDate,
            end_date: endDate,
          }),
        },
        token,
      );
    } catch (error) {
      console.error("Run backtest failed:", error);
    }
    onOpenChange(false);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogTrigger asChild>
        <Button className="gap-1.5">
          <Play className="size-3.5" />
          Run Backtest
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Run New Backtest</DialogTitle>
          <DialogDescription>
            Configure and launch a historical backtest simulation
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4 py-2">
          <div className="space-y-2">
            <Label>Strategy</Label>
            <Select
              value={selectedStrategy}
              onValueChange={setSelectedStrategy}
            >
              <SelectTrigger>
                <SelectValue placeholder="Select strategy..." />
              </SelectTrigger>
              <SelectContent>
                {strategies.map((s) => (
                  <SelectItem key={s.id} value={s.id}>
                    {s.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-2">
            <Label>Instruments</Label>
            <Input
              value={instruments}
              onChange={(e) => setInstruments(e.target.value)}
              placeholder="AAPL, MSFT, SPY"
            />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-2">
              <Label>Start Date</Label>
              <Input
                type="date"
                value={startDate}
                onChange={(e) => setStartDate(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label>End Date</Label>
              <Input
                type="date"
                value={endDate}
                onChange={(e) => setEndDate(e.target.value)}
              />
            </div>
          </div>
          <div className="space-y-2">
            <Label>Configuration (JSON)</Label>
            <Textarea
              value={config}
              onChange={(e) => setConfig(e.target.value)}
              className="h-32 font-mono text-sm"
              placeholder='{ "fast_period": 12, "slow_period": 26 }'
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button className="gap-1.5" onClick={handleRunBacktest}>
            <FlaskConical className="size-3.5" />
            Run Backtest
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
