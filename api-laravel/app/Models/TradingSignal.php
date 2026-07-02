<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;

class TradingSignal extends Model
{
    protected $table = 'trading_signals';
    
    // Python engine handles timestamps, disable Eloquent's default created_at/updated_at
    public $timestamps = false;

    // Allow reading/writing all columns
    protected $guarded = [];
}
