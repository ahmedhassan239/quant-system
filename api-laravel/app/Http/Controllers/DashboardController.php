<?php

namespace App\Http\Controllers;

use Illuminate\Http\Request;
use App\Models\ActiveSymbol;
use App\Models\MacroState;
use App\Models\Position;

class DashboardController extends Controller
{
    public function stats()
    {
        $activePositionsCount = Position::where('asset_balance', '>', 0)->count();
        $totalPnl = Position::sum('pnl_usd'); 
        
        $winningTrades = Position::where('pnl_usd', '>', 0)->count();
        $totalTrades = Position::count() ?: 1; // Avoid division by zero
        $winRate = round(($winningTrades / $totalTrades) * 100, 2);

        return response()->json([
            'total_pnl' => $totalPnl,
            'win_rate' => $winRate,
            'active_positions_count' => $activePositionsCount
        ]);
    }

    public function macroTrends()
    {
        return response()->json(MacroState::all());
    }

    public function symbols()
    {
        return response()->json(ActiveSymbol::where('is_active', true)->get());
    }

    public function addSymbol(Request $request)
    {
        $validated = $request->validate([
            'symbol' => 'required|string|unique:quant_shared.active_symbols,symbol'
        ]);

        $symbol = ActiveSymbol::create([
            'symbol' => strtoupper($validated['symbol']),
            'is_active' => true
        ]);

        return response()->json(['message' => 'Symbol added successfully', 'data' => $symbol]);
    }
}
