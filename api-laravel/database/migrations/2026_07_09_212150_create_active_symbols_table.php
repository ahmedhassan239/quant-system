<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        Schema::connection('quant_shared')->create('active_symbols', function (Blueprint $table) {
            $table->id();
            $table->string('symbol')->unique();
            $table->boolean('is_active')->default(true);
            $table->timestamps();
        });
    }

    public function down(): void
    {
        Schema::connection('quant_shared')->dropIfExists('active_symbols');
    }
};
