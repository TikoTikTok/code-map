<?php
namespace App\Models;

/** A user model. */
class User
{
    private string $name;

    public function __construct(string $name)
    {
        $this->name = $name;
    }

    /** The display name. */
    public function name(): string
    {
        return $this->name;
    }
}
