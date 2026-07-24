<?php

namespace App\Http\Controllers\Api;

use App\Http\Controllers\Controller;
use App\Models\MarketData;
use App\Models\TradingSignal;
use App\Models\BotLog;
use Illuminate\Http\Request;

class DashboardController extends Controller
{
    public function logs()
    {
        try {
            $logs = BotLog::orderBy('created_at', 'desc')->take(100)->get();
            return response()->json(array_reverse($logs->toArray()));
        } catch (\Exception $e) {
            \Illuminate\Support\Facades\Log::error("Api/DashboardController logs error: " . $e->getMessage());
            return response()->json([], 200);
        }
    }

    public function index()
    {
        try {
            // Fetch the latest 100 records from the market_data table ordered by timestamp ASC
            $marketData = MarketData::orderBy('timestamp', 'asc')->take(100)->get();
            
            // Fetch the latest 5 records from the trading_signals table ordered by timestamp DESC
            $latestSignals = TradingSignal::orderBy('timestamp', 'desc')->take(5)->get();

            return response()->json([
                'market_data' => $marketData,
                'latest_signals' => $latestSignals
            ]);
        } catch (\Exception $e) {
            \Illuminate\Support\Facades\Log::error("Api/DashboardController index error: " . $e->getMessage());
            return response()->json([
                'market_data' => [],
                'latest_signals' => []
            ], 200);
        }
    }
}
