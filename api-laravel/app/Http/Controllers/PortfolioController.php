<?php

namespace App\Http\Controllers;

use Illuminate\Support\Facades\DB;

class PortfolioController extends Controller
{
    public function balance()
    {
        try {
            // Query the wallet_balance table directly from the shared DB connection
            $record = DB::table('wallet_balance')->first();
            $balance = $record ? (float) $record->balance : 0.00;

            return response()->json([
                'status' => 'success',
                'wallet_balance' => $balance
            ]);
        } catch (\Exception $e) {
            \Illuminate\Support\Facades\Log::error("PortfolioController balance error: " . $e->getMessage());
            return response()->json([
                'status' => 'success',
                'wallet_balance' => 0.00
            ], 200);
        }
    }
}
