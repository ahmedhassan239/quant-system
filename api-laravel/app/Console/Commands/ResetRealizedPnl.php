<?php

namespace App\Console\Commands;

use Illuminate\Console\Command;
use Illuminate\Support\Facades\DB;

class ResetRealizedPnl extends Command
{
    /**
     * The name and signature of the console command.
     *
     * @var string
     */
    protected $signature = 'quant:reset-pnl {--force : Force reset without interactive confirmation}';

    /**
     * The console command description.
     *
     * @var string
     */
    protected $description = 'Reset Total Realized PNL to $0.00 by truncating trade history while protecting active positions';

    /**
     * Execute the console command.
     */
    public function handle()
    {
        $this->info("═════════════════════════════════════════════════════════════════");
        $this->info(" 🛠️  REALIZED PNL & TRADE HISTORY RESET (LARAVEL ARTISAN)");
        $this->info("═════════════════════════════════════════════════════════════════");

        try {
            $oldTradesCount = DB::table('trade_history')->count();
            $oldPnl = (float) DB::table('trade_history')->sum('pnl_usd');
            $activePosCount = DB::table('positions')->count();

            $this->info("📊 STATE BEFORE RESET:");
            $this->line("   • Closed Trades Count : {$oldTradesCount}");
            $this->line("   • Total Realized PNL  : $" . number_format($oldPnl, 2));
            $this->line("   • Active Open Trades  : {$activePosCount} (STRICTLY PROTECTED)");
            $this->line("─────────────────────────────────────────────────────────────────");

            if ($oldTradesCount === 0) {
                $this->info("ℹ️  Trade history is already empty. Realized PNL is $0.00.");
                return 0;
            }

            if (!$this->option('force') && !$this->confirm("Are you sure you want to wipe {$oldTradesCount} historical trades and reset Realized PNL to $0.00?")) {
                $this->warn("Aborted reset operation.");
                return 0;
            }

            $this->line("⏳ Wiping historical trades (truncating trade_history)...");
            
            // Perform clean truncate in PostgreSQL
            DB::statement('TRUNCATE TABLE trade_history RESTART IDENTITY;');

            $newTradesCount = DB::table('trade_history')->count();
            $newPnl = (float) DB::table('trade_history')->sum('pnl_usd');
            $newActivePosCount = DB::table('positions')->count();

            $this->line("─────────────────────────────────────────────────────────────────");
            $this->info("📈 VERIFICATION AFTER RESET:");
            $this->line("   • Closed Trades Count : {$newTradesCount}");
            $this->line("   • Total Realized PNL  : $" . number_format($newPnl, 2));
            $this->line("   • Active Open Trades  : {$newActivePosCount} (VERIFIED UNTOUCHED)");
            $this->info("═════════════════════════════════════════════════════════════════");

            if ($newTradesCount === 0 && $newPnl == 0.0 && $newActivePosCount === $activePosCount) {
                $this->info("🎉 SUCCESS: Total Realized PNL cleanly reset to $0.00!");
                return 0;
            } else {
                $this->error("⚠️ WARNING: Verification discrepancy detected!");
                return 1;
            }
        } catch (\Exception $e) {
            $this->error("❌ CRITICAL ERROR during PNL reset: " . $e->getMessage());
            return 1;
        }
    }
}
