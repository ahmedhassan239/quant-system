<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Factories\HasFactory;

class BotLog extends Model
{
    use HasFactory;

    protected $table = 'bot_logs';
    
    protected $fillable = [
        'symbol',
        'action',
        'message',
    ];
}
